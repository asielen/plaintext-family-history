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
  4. Then the same size-first comparison among the incoming files themselves, so
     one recording handed over twice under two names is imported once.

Equal size is common and proves nothing; equal SHA-256 over the whole file is
what "byte-identical" means here, and it is the only thing this script will call
a duplicate.

THE COVERAGE INVARIANT
======================
This script is a safety gate: the skill imports what it clears. The tempting
reading of its job is "did I find a twin?", and that reading is what failed, five
review rounds running. Each round found a different route to a confident `new` -
a candidate that could not be hashed, a config spelling that did not parse, a
dot-directory pruned from the walk, a subtree behind a symlink, a root that was
not there - and each one was the same shape underneath: a path that quietly
examined less than the whole archive and still answered.

So the question the gate actually answers is a coverage question:

    DID I EXAMINE EVERYTHING I AM CLAIMING TO HAVE EXAMINED?

`new` is a positive statement with five parts, and all five must hold:

  ROOTS        every media root the archive configures was resolved and is a
               readable folder. A configured root that is not there right now is
               not an empty root; its recordings exist, unread.
  ENUMERATION  every file under every root was listed - hidden folders, folders
               behind directory symlinks, all of them. A subtree that is skipped
               has exactly one honest verdict, and it is not "new".
  DOMAIN       both sides are filtered by the SAME media rule (`is_media`), and
               every input the human named was either checked or named out loud
               as not checked. A file the archive side would never list cannot be
               cleared against it. "Archived" means FILED: the inbox is staging,
               which the archive may keep inside the photo library, and a file
               waiting there is the opposite of one already imported.
  CANDIDATES   every archived file of exactly the incoming byte size was opened
               and hashed. One that could not be read is an open question.
  BATCH        the incoming files were compared against each other as well. One
               afternoon exported twice under two names is two `new` verdicts
               that are each true and together wrong: `new` means "import this
               one", and importing both splits one sitting across two records.

Break any part and the answer is `indeterminate`, not `new`. There is one place
each part is decided (`resolve_media_roots`, `index_sizes_by_root` /
`expand_inputs`, `is_media` applied on both sides, `check_one`,
`mark_bundle_repeats`) and one place the verdicts are held to it
(`apply_archive_coverage`). A new failure mode belongs in whichever of those
already owns its part, never in a fresh special case beside them - rounds of
special cases is what got us here.

That direction matters more than it looks. The cost of a wrong `indeterminate`
is that a human plugs a drive back in and runs one command again. The cost of a
wrong `new` is a second source record for one recording, with that afternoon's
claims split between two S-ids and nothing in either record saying so.

The mirror of that rule: a file the human hands over that is ALREADY inside a
media root is not a file with no twin, it is the archive's own copy. It is
reported as already filed, never cleared.

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
compared against every incoming recording, every archived media file, the
archive's own fha.yaml, and the media roots themselves BEFORE anything is
hashed, and the run is refused outright on a collision. See
`report_path_collision`.

TWO NAMES, ONE FILE
===================
Every question above is really a question about path identity - is this
incoming file the archived one, is this folder the inbox, would the report land
on a recording - and a path is a bad way to ask it. One file has many names: a
case variant on a case-insensitive volume (the default on macOS and Windows), an
accent stored NFD by the filesystem and typed NFC by the human, a symlink, a
hard link, a second mount of the same disk. Comparing the strings answers a
different question than the one asked, and it answers it wrong in whichever
direction the spelling happened to fall.

There is no single right comparison, because the two mistakes cost different
things in different places, so this file uses three and picks by consequence:

  same_file            the filesystem's own answer, (device, inode). Exact, and
                       the only one used where a wrong MATCH is the dangerous
                       direction - deciding that a folder is one already walked,
                       or that two configured roots are one root.
  could_be_same_file   samefile when both paths exist, and a blunt case-folded,
                       NFC-normalised comparison when one does not (a report
                       that has not been written yet has no inode to compare).
                       Used only for `--json`, where a wrong match costs one
                       sentence and a missed match costs a recording.
  canonical_path       string tidying only - absolute, symlinks resolved. Still
                       the right tool where a wrong match would DROP something:
                       an input the human named, or a same-size candidate. A
                       hard link is a second name for a filed recording, and
                       treating it as "the file itself" would clear it as new.

`walk_covering` has always used the (device, inode) idiom for its loop guard.
The bug worth remembering is that the knowledge stayed there: everything else
compared strings through an `os.path.normcase` that folds case on Windows only,
under a docstring promising it folded on macOS too.

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

Inside a media root, EVERY folder is walked - dot-prefixed ones included, and
folders reached through a directory symlink included. A `documents/.private/` is
the human's own folder and may hold exactly the recording being checked, and a
`documents/interviews -> /Volumes/Audio/interviews` is how a great many people
keep a big library where it already lives. `.git/`, `.fha/` and `.cache/` hold no
media file, so walking them changes nothing but a few directory listings.
Following links needs a loop guard, and the guard is the one already required by
the coverage rule: a directory this run has enumerated once is not enumerated
again, which both ends the loop and loses nothing. See `index_sizes_by_root`.

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
  2  at least one incoming file is byte-identical to a filed recording, or to
     another file in the same batch; the skill's rule is to report that item and
     skip it, never to import one recording twice
  3  the check could not be completed for at least one file (something could not
     be read). Nothing in the run is cleared for import; fix what is named and
     run the same command again
  1  usage or configuration error (bad path, unreadable archive root, an
     fha.yaml that will not parse, PyYAML not installed)

CODE MAP
========
  Identity    canonical_path / _fold / _identity_key / fs_identity /
              same_file / could_be_same_file / _is_inside / _relative_under
  Config      find_archive_root / _roots_from_config / resolve_media_roots
  Naming      media_root_label / build_named_roots / portable_path
  Index       is_media / walk_covering / index_sizes_by_root / sha256_file /
              source_id_in
  Check       expand_inputs / filed_inside_media_root / check_one /
              mark_bundle_repeats / apply_archive_coverage
  Safety      report_path_collision
  CLI         build_parser / fail / main
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import unicodedata

TOOL_NAME = "find_duplicate_media.py"
TOOL_VERSION = "1.5.0"

REPORT_EXAMPLE = "dedupe-report.json"

# Audio and video are the same job here: a video is a recording like any other.
MEDIA_EXTENSIONS = {
    ".aac", ".aif", ".aiff", ".amr", ".flac", ".m4a", ".m4b", ".mp3", ".oga",
    ".ogg", ".opus", ".wav", ".wma",
    ".3gp", ".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm",
    ".wmv",
}

# Roots that can hold a FILED recording. `documents` is where interviews live;
# `photos` is included because a phone video can legitimately have been filed
# there before anyone thought of it as an interview. These two are the whole
# list: `_lib.ASSET_ROOT_ALIASES` is `photos, documents, inbox`, and a source
# record's `files:` entries only ever resolve under the first two.
MEDIA_ROOT_ALIASES = ("documents", "photos")

# Staging, which is the opposite of filed. The inbox holds what has NOT been
# imported yet, and SPEC 12.4 explicitly allows it to sit inside the photo
# library's own workflow (`inbox: C:/Photos/_inbox`) - so a file under it can be
# inside a media root while being the very thing the human is asking us to
# import. Everything below treats the staging subtree as not-archived.
STAGING_ROOT_ALIAS = "inbox"

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
# Path identity: which of two names are one file
# ---------------------------------------------------------------------------
def canonical_path(path):
    """One absolute, symlink-resolved spelling of a path. A string, no more.

    `./report.json` and `report.json` are the same file, and a symlink is
    whatever it points at. That is as far as tidying a STRING can get you. On a
    case-insensitive volume `Report.json` and `report.json` are also one file,
    and no amount of string work can tell you so, because the answer lives in
    the filesystem and not in the name. `os.path.normcase` is kept here because
    it is genuinely right on Windows, where the OS folds case itself; on macOS
    and Linux it returns the path unchanged, which is why this function is a
    normaliser and never an identity test. Identity goes through `same_file`
    (exact) or `could_be_same_file` (blunt, for a path not yet written).

    It is still the right comparison in the two places where a wrong match
    would DROP something - `expand_inputs` and `check_one` - and each says so
    where it uses it.

    Restated rather than imported from the sibling attribute_speakers.py, for
    the same reason `find_archive_root` is restated: each of this skill's
    scripts has to run standalone from any directory, on nothing but the
    standard library. The two are no longer the same function: that copy still
    decides `--out`/`--report` collisions by this string alone, under the
    docstring promise of macOS case equivalence that `normcase` never kept, so
    it needs the same two-armed check this file grew. Do not re-sync them by
    copying this one back - the collision sites there are what need changing.
    """
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _fold(name):
    """One filename with its case and its Unicode spelling flattened away.

    Two things make one name look like two. A case-insensitive volume - the
    default on macOS and on Windows - stores `Interview.m4a` and gives it back
    to whoever asks for `interview.m4a`. And HFS+ stores its directory entries
    decomposed (NFD), so a name typed or pasted as NFC comes back from the disk
    as a different Python string with the same accents. NFC-normalising before
    folding settles the second; `casefold` settles the first.

    `str.casefold` is more eager than any filesystem's own folding table, and
    that is on purpose: it is only ever used where matching too much costs a
    sentence and matching too little costs a recording.
    """
    return unicodedata.normalize("NFC", name).casefold()


def _identity_key(path):
    """A deliberately blunt key: any two names for one file share it.

    Folded on every platform rather than after probing the volume, because the
    two mistakes are not the same size where this is used (`--json`): a wrong
    match tells the human to pick another filename for his report, and a missed
    match overwrites the recording the report had just cleared for import. A
    probe is also weaker than it sounds - it has to be run on the volume
    holding each candidate (documents and photos are allowed to sit on
    different drives), it needs a write to be trustworthy, and a read-only
    mount cannot answer it at all.
    """
    return _fold(canonical_path(path))


def _identity_from_stat(st, path):
    """(device, inode) for a stat already taken, or the canonical string.

    Some network and Windows filesystems report an inode of 0 for everything,
    and a key that collides for every file would be worse than no key at all -
    it would prune an entire walk. There the canonical string is the best
    available answer, and it is a string, so it can never collide with a real
    (device, inode) pair held in the same set.
    """
    return (st.st_dev, st.st_ino) if st.st_ino else canonical_path(path)


def fs_identity(path):
    """The filesystem's own name for a file or folder, for use as a dict key.

    The one identity that survives every way a path can be spelled. Callers
    that have already stat'd the path use `_identity_from_stat` directly; this
    is for callers that have not, and it answers with the canonical string when
    the path cannot be stat'd at all, leaving the error to the caller's own
    unreadable-path handling.
    """
    try:
        st = os.stat(path)
    except OSError:
        return canonical_path(path)
    return _identity_from_stat(st, path)


def same_file(a, b):
    """Do these two names point at one file, as the filesystem sees it?

    `os.path.samefile` compares (device, inode) - the filesystem's own answer,
    and correct on a case-insensitive volume, through a symlink, through a hard
    link, and across two mounts of one disk, none of which a string comparison
    can see. It needs both paths to exist and raises OSError when one does not,
    which is not a problem to work around: a file that is not there cannot be
    the file in hand. The string comparison is kept as the fallback so that two
    spellings of one absent path still compare equal.

    Used where a wrong TRUE is the dangerous direction and both paths exist:
    the folder-identity comparisons in `_is_inside`, and the de-duplication of
    configured roots. `could_be_same_file` is for the other direction.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return canonical_path(a) == canonical_path(b)


def could_be_same_file(a, b):
    """Could writing to one of these land on the other? Two arms, one plan.

    A report path is usually a file that does not exist yet, and `samefile`
    cannot speak about a file that is not there. So the check has two arms:

      * both exist - ask the filesystem. Authoritative on every volume, and it
        catches the aliases no string can: a hard link, a second mount of the
        same disk, a case variant on a folding volume where only one of the two
        names is the one on disk.
      * one does not - compare the blunt key instead: the same parent folder
        after symlinks are resolved, and the same name once case and Unicode
        form are folded away (`_identity_key`).

    Blunt on purpose, and only for `--json`. This is the answer to "is it safe
    to write here", where the two mistakes cost a sentence and a recording.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return _identity_key(a) == _identity_key(b)


def _is_inside(path, root):
    """Is `path` at or below `root`, as the filesystem sees it?

    Containment decides real things here - whether a handed-over recording is
    the archive's own copy, whether a folder is the inbox rather than the
    library, whether a `--json` path would write inside the archive - so it is
    answered by identity rather than by string prefix. Two spellings of one
    folder are one folder, and a prefix test reads them as two: the answer
    flips, and the verdict flips with it. A file that is inside the archive
    gets reported as outside it, which is how a filed recording gets cleared
    for a second import.

    The walk is upwards. Resolve both sides, then compare `path` and each
    folder above it against `root` until they meet or there are no parents
    left. Walking up is also what answers for a path that does NOT exist yet -
    a report about to be written - because the climb reaches the folder that
    will hold it, and a file is inside `root` exactly when the nearest existing
    folder above it is.

    The string prefix test is kept as a fast first answer. Both sides are
    resolved by then, so when it matches, the containment is real.
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


def _path_parts(path):
    """An absolute path split into its components, separators normalised."""
    return [part for part in
            os.path.abspath(path).replace("\\", "/").split("/") if part]


def _relative_under(target, root, fold=False):
    """`target` written relative to `root`, or None when it is not below it.

    Compared component by component rather than by string prefix, because a
    folded comparison is per NAME: folding can change a string's length ('ss'
    for 'ß'), so the number of characters `root` occupies inside `target` is
    not the number of characters `root` has, and slicing by the wrong length
    would cut a reported path in the middle of a folder name. Comparing the
    names and then joining the ORIGINAL remainder keeps the reported path
    spelled the way the file on disk is spelled.
    """
    parts = _path_parts(target)
    base = _path_parts(root)
    if len(parts) < len(base):
        return None
    for part, base_part in zip(parts, base):
        if part != base_part and not (fold and _fold(part) == _fold(base_part)):
            return None
    return "/".join(parts[len(base):])


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
    # An alias written with no value (`documents:` and nothing after it) is kept
    # as an empty string rather than dropped. Dropping it would make an explicit
    # but broken setting indistinguishable from a setting nobody wrote, and
    # `resolve_media_roots` decides between those two on exactly this key.
    return {str(k): ("" if v is None else str(v)) for k, v in roots.items()}


def resolve_media_roots(archive_root):
    """Named media roots to search, plus any configured root that is not there.

    Returns `(named_roots, missing, staging)`. `named_roots` is a list of
    `(alias, absolute path)` - the alias is the archive's own name for the root
    (`documents`, `photos`), which is what every reported path is rendered
    against. `staging` is the archive's inbox folder, resolved the same way and
    returned alongside because the inbox is allowed to live INSIDE a media root;
    everything under it is waiting to be imported, not filed, and the rest of
    this script has to be able to tell the difference.

    Resolved through `roots:` rather than joined onto the archive root, because
    an external documents root is normal (AGENTS.md: "asset roots may live
    OUTSIDE this folder"). A hardcoded `<archive>/documents` on such an archive
    finds zero files and reports every incoming recording as new.

    This is the ROOTS half of the coverage invariant, and the whole of it: after
    this function returns with an empty `missing`, every folder the archive says
    holds recordings is a folder this run can walk.

    WHAT COUNTS AS MISSING, AND THE ONE CASE THAT DOES NOT
    A root goes into `missing` when fha.yaml NAMES it and it is not a readable
    folder right now - whether it names an external drive nobody plugged in, a
    path that moved, an internal folder somebody renamed, a file where a folder
    should be, or an alias written with no value at all. In every one of those
    the archive has stated that recordings live there, so its absence hides
    recordings and nothing can be cleared against it.

    The single case that is ordinary: an alias fha.yaml does NOT mention, whose
    built-in default folder is absent inside the archive. A young archive with no
    `photos/` yet has no photos anywhere, so nothing was hidden by not looking,
    and refusing the run would be refusing it forever. It appears in neither
    list. (The shipped `archive-template/fha.yaml` writes both aliases and both
    folders, so an archive made from it is never in this state by accident - a
    configured internal root that has gone missing really has gone missing.)

    An earlier version tested only "does the path exist, or does it point outside
    the archive". That let an explicitly configured `documents: documents` whose
    folder had been renamed fall through as if it were an unconfigured default:
    the run scanned `photos/` alone, found no twin in it, and cleared the whole
    bundle on exit 0 with the interview library untouched and unread.
    """
    roots = _roots_from_config(archive_root)
    named = []
    missing = []
    for alias in MEDIA_ROOT_ALIASES:
        configured = alias in roots
        value = roots.get(alias, alias)
        if configured and not value.strip():
            missing.append((alias, "no folder named in fha.yaml"))
            continue
        base = value if os.path.isabs(value) else os.path.join(archive_root, value)
        base = os.path.abspath(base)
        if os.path.isdir(base):
            # Both folders exist by here, so `same_file` is exact: one folder
            # configured twice under two aliases (or two spellings of one
            # folder) is one root, and listing it twice would render the same
            # filed recording under whichever alias sorted first.
            if not any(same_file(base, existing) for _label, existing in named):
                named.append((alias, base))
        elif configured or os.path.exists(base) or not _is_inside(base, archive_root):
            missing.append((alias, value))
    staging_value = roots.get(STAGING_ROOT_ALIAS) or STAGING_ROOT_ALIAS
    staging = (staging_value if os.path.isabs(staging_value)
               else os.path.join(archive_root, staging_value))
    return named, missing, os.path.abspath(staging)


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

    An incoming argument that is itself inside a media root gets no `incoming`
    label. It would win the longest-root sort and rename the archive's own folder
    for the length of one run, so a recording the human handed back from
    `documents/interviews/` would be printed as `incoming/…` on the very line
    saying it is already filed. Where a file really sits is the more useful of
    the two facts, and the only one the reader can check.
    """
    named = list(media_roots)
    if archive_root:
        named.append(("", os.path.abspath(archive_root)))
    seen = []
    count = 0
    for arg in incoming_args:
        arg_abs = os.path.abspath(arg)
        base = arg_abs if os.path.isdir(arg_abs) else os.path.dirname(arg_abs)
        # Every one of these folders exists - main() checked the argument before
        # calling - so the folder identity is exact. Two spellings of one bundle
        # folder would otherwise take two labels, and the file the human handed
        # over would be rendered under whichever one happened to sort first.
        if any(same_file(base, done) for done in seen):
            continue
        seen.append(base)
        if any(_is_inside(base, root) for _label, root in media_roots):
            continue
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

    Three passes over the roots, most trustworthy spelling first, because a
    later pass must never take a path away from the root that really holds it -
    on a case-sensitive volume `Documents/` and `documents/` are two folders,
    and only one of them holds this file:

      1. exact. Every archived path came off the walk of a root, so it is
         already spelled the way that root is, and this is the only pass they
         ever need.
      2. folded. For the paths the human typed himself, which are the only ones
         here the walk did not produce.
      3. resolved. For a path that reaches a root through a directory link -
         `~/phone-export -> documents/interviews` - where no spelling of the
         name can show the relationship and only the filesystem knows.

    Falling past all three means a bare filename, which is the one thing this
    function must not do to a file that a named root does hold: `recording.m4a`
    cannot tell the human WHICH of three folders holding that name matched.
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
                return label or "."
            return "%s/%s" % (label, rel) if label else rel
    return os.path.basename(target)


# ---------------------------------------------------------------------------
# Size index and hashing
# ---------------------------------------------------------------------------
def is_media(path):
    """The one media rule, applied identically to both sides of the comparison.

    The archive side indexes what this accepts, so the incoming side can only be
    answered for what this accepts. A `.txt` handed in directly has nothing in
    the index to be compared against, and a recording in a container this set
    does not list is invisible on both sides at once - either way the honest
    move is to say so rather than to return a verdict, which is what
    `expand_inputs` does with what it rejects.
    """
    return os.path.splitext(path)[1].lower() in MEDIA_EXTENSIONS


def walk_covering(root, unreadable, visited):
    """Yield `(dirpath, filenames)` for every folder at or below `root`.

    This is the ENUMERATION half of the coverage invariant, written once and used
    by both walks - the archive index and the incoming bundle - because a rule
    that holds on one side and not the other is how the incoming half kept
    drifting out of step with the archived half.

    Three things it does that a plain `os.walk` does not:

    * FOLLOWS DIRECTORY SYMLINKS. `os.walk` defaults to `followlinks=False`, and
      it skips such a subtree in silence: nothing is listed and nothing is
      recorded as unread, so an archived recording under
      `documents/interviews -> /Volumes/Audio/interviews` simply is not there as
      far as the gate can tell, and its incoming copy comes back `new`.
    * ENUMERATES EACH FOLDER ONCE. Following links means loops
      (`documents/loop -> documents`) and diamonds (two links onto one folder).
      A folder already enumerated this run is pruned, which ends the loop without
      losing coverage: everything under it is already in hand. The identity is
      the folder's own (device, inode) - see `_identity_from_stat`, which is
      where this file's one correct idea about path identity lived alone for
      too long.
    * REPORTS WHAT IT COULD NOT ENTER. Both the directory-listing errors
      `os.walk` hands to `onerror` and a folder that cannot be stat'd land in
      `unreadable`, which is what turns a partial walk into `indeterminate`
      instead of into a short list nobody notices.

    `visited` is the caller's set, shared across roots on purpose: two configured
    roots where one nests inside the other are enumerated once between them.
    """

    def on_walk_error(err):
        unreadable.append(getattr(err, "filename", None) or "a folder")

    for dirpath, dirnames, filenames in os.walk(
            root, onerror=on_walk_error, followlinks=True):
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


def index_sizes_by_root(named_roots, staging=None):
    """Map byte size -> [archived media paths of that size], plus what failed.

    Sizes come from `os.scandir`'s stat, which the directory walk already has,
    so building this index opens no files at all. It is the cheap half of the
    check; hashing only happens for the sizes that actually collide.

    Returns `(by_size, unreadable)`. An archived file or folder that cannot be
    read is NOT the same thing as one that is not a twin: it is a file this run
    never saw. Every such path is returned so the caller can refuse to clear
    anything, because a recording sitting in an unlistable folder is exactly
    where a byte-identical twin would hide.

    NO FOLDER IS SKIPPED, INCLUDING DOT-PREFIXED AND SYMLINKED ONES. Two
    earlier versions skipped one each: the first pruned every directory whose
    name began with a dot, dropping `documents/.private/interview.m4a` - a
    folder a human makes for exactly the material he is most careful about; the
    second let `os.walk` decline to follow a directory symlink, dropping a
    library kept where it already lives. Both dropped the subtree WITHOUT
    landing it in `unreadable`, so the twin arrived, matched nothing, and was
    cleared as new. There is no third option for a skipped subtree: it can only
    be honest as `indeterminate`, and an archive that reports indeterminate on
    every run because it happens to contain a `.git` is a gate nobody obeys.
    So `walk_covering` walks everything. The machine-owned dot-folders an
    archive actually has - `.fha/` (vendored tools), `.cache/` (sqlite),
    `.git/` - hold no file with a media extension, so walking them costs one
    directory listing each and changes no verdict; every other dot-folder in a
    media root is the human's own and may well hold a filed recording.

    THE ONE SUBTREE THAT IS NOT ARCHIVED. `staging` is the archive's inbox. It
    is skipped, and skipping it loses no coverage because this index answers
    "what is already FILED": a recording in the inbox has no source record and
    no S-id, and calling it a twin would tell the human that the recording he is
    trying to import is already in the archive - stopping the very import that
    would file it. This matters only when the inbox is configured inside a media
    root, which SPEC 12.4 allows; the default `<archive>/inbox` is outside both
    roots and never comes up. It is a statement about what "archived" means, not
    a folder the walk failed to reach, which is why it is here and not a prune
    inside `walk_covering`.

    `photos_ignore:` is deliberately NOT honoured, and that is not an oversight
    of the "a knob that filters a tree reaches every walker" rule. It tells the
    photo CATALOG which material is not the archive's subject; it does not
    unfile anything. A recording sitting in an ignored folder is still on disk,
    still attached to a source record, and still exactly the twin an incoming
    file might be - so honouring the pattern here would reintroduce the pruned
    subtree this function's whole history is about.
    """
    by_size = {}
    unreadable = []
    visited = set()
    if staging:
        # The inbox is excluded at its own folder identity, which prunes
        # everything below it in one step - and recognises it however the walk
        # arrives, through a link or under a spelling that differs from the one
        # fha.yaml uses. `walk_covering` prunes what is already in `visited`;
        # putting the inbox there before the walk starts says "this subtree is
        # not the archive" in the vocabulary the walk already speaks.
        visited.add(fs_identity(staging))

    for _label, root in named_roots:
        for dirpath, filenames in walk_covering(root, unreadable, visited):
            # The identity prune above cannot see a link that lands BELOW the
            # inbox's own folder (`documents/x -> inbox/monday`), which arrives
            # without ever passing through the inbox itself.
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


def sha256_file(path, cache):
    """SHA-256 of a whole file, memoised per run.

    Whole-file, not a sampled prefix: two recordings of the same conversation
    from the same app share long identical headers, and a prefix hash would call
    them the same file. The cache matters because one incoming folder is often
    checked against the same size-colliding archived candidate many times over.

    Keyed by filesystem identity, so a candidate reached through a second name -
    a link, a hard link, a case variant of a name already read - is recognised
    as the file whose digest is already in hand. This is the one place a blunter
    key would be harmless anyway: every possible wrong hit returns the digest of
    the very same bytes.
    """
    key = fs_identity(path)
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

    Returns `(files, unreadable, not_media)` - the DOMAIN half of the coverage
    invariant, which is the promise that every path the human named is accounted
    for in exactly one of those three lists.

    * `files` are what gets a verdict. The media rule is applied here to a named
      file exactly as it is inside a walked folder. It used to be applied only
      inside the folder walk, so an explicitly named `notes.txt` was counted as a
      checked recording and cleared as `new` - a verdict drawn from an index that
      never lists a file like it, and so from nothing at all.
    * `not_media` is what the gate cannot speak about. The caller prints it, so
      the human is told plainly that nothing was checked for that file rather
      than being handed a clearance for it. (Non-media files found by walking a
      folder are not listed: skipping the transcripts inside a bundle is what the
      human expects, while silence about a file he named himself is not.)
    * `unreadable` is what could not be examined: a subfolder that cannot be
      listed, and a named path that is neither a file nor a folder. The
      recordings inside it were never checked, and a bundle imported wholesale
      carries them past the gate unexamined.

    The walk is `walk_covering`, the same one the archive side uses, so hidden
    folders and folders behind directory symlinks are covered on both sides by
    one rule. A recording under `incoming/.old/` or `incoming/link -> elsewhere`
    that this function never returns is a recording the human's bundle still
    contains and `fha process` will still file, so leaving it out of the list is
    the gate saying nothing about a file it is about to wave through.
    """
    out = []
    unreadable = []
    not_media = []
    visited = set()

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
    seen = set()
    unique = []
    for p in sorted(out):
        # Canonical, not just absolute: following directory links means one file
        # can be reached by two names in one run, and checking it twice would
        # report "checked 3 recordings" for a bundle holding two.
        #
        # And canonical rather than the filesystem's (device, inode), which is
        # the sharper identity everywhere else in this file. Here the sharper
        # answer is the wrong one twice over. Two hard links in one bundle are
        # two names the human is about to import, and dropping one of them
        # would leave a file that gets filed with nothing said about it - while
        # keeping both costs only a line saying they are the same recording
        # (`mark_bundle_repeats`, which is exactly that job). A blunt folded key
        # is wrong for the mirror reason: on a case-sensitive volume `A.m4a`
        # and `a.m4a` really are two recordings, and folding them together
        # would drop one from the run unexamined. The cost of over-listing here
        # is a duplicated line; the cost of under-listing is a recording nobody
        # checked, so this comparison stays the one that over-lists.
        key = canonical_path(p)
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique, unreadable, not_media


def filed_inside_media_root(path, media_roots, staging=None):
    """The archived path of an incoming file that already lives in the archive.

    Returns that path, or None when the file is genuinely from outside - which
    includes anything under `staging`, the archive's inbox. The inbox may sit
    inside the photo library (SPEC 12.4), and a file waiting there is precisely
    a file that has NOT been imported; "already filed, nothing to import" is the
    wrong answer for the whole capture workflow.

    `check_one` has to drop the incoming file from its own candidate list - a
    file is not its own duplicate - and that filter could not tell two cases
    apart. One is harmless: an incoming bundle staged inside a media root, where
    each file would otherwise match itself. The other is the reason this function
    exists: the human selects a recording the archive has ALREADY filed (or hands
    over the whole `documents/interviews/` folder) and asks whether to import it.
    Removing it left an empty candidate list, and an empty candidate list read as
    `new` - the gate authorising a second import of the very file it was
    pointed at.

    Membership is decided by `_is_inside`, which asks the filesystem which
    folder holds what: a shortcut or a symlink into the archive is recognised as
    the archive's own copy rather than as a stranger that happens to hash the
    same, and a path the human typed with different capitalisation than the
    archive uses is still the archive's own folder. Getting that wrong in
    either direction changes the answer the human is given - "already filed" for
    a file waiting in the inbox, or a clean import for a recording the archive
    already holds.
    """
    target = os.path.realpath(os.path.abspath(path))
    if staging and _is_inside(target, staging):
        return None
    for _label, base in media_roots:
        base_real = os.path.realpath(os.path.abspath(base))
        if target != base_real and _is_inside(target, base_real):
            return target
    return None


def check_one(path, by_size, cache, media_roots=(), staging=None):
    """Result dict for one incoming file: duplicate, new, or indeterminate.

    Three verdicts, not two, and the distinction is the whole safety story.
    "new" authorises an import, so it is only ever returned when every same-size
    archived candidate was actually opened and hashed and none of them matched -
    the CANDIDATES half of the coverage invariant. A candidate that could not be
    read leaves the question open, and an open question is `indeterminate`: the
    unreadable candidates are listed on the entry, and the caller exits nonzero
    rather than clearing the file.

    Rounding an unreadable candidate down to "not a twin" is how a byte-
    identical recording gets imported a second time - one afternoon under two
    S-ids, its claims split between them, and nothing in either record saying so.

    `media_roots` is optional so the function stays callable with a bare size
    index in a unit test; passing it is what lets an incoming file that is
    already filed be recognised as such (see `filed_inside_media_root`) rather
    than compared against the archive it is part of.
    """
    entry = {"file": os.path.basename(path), "verdict": "new",
             "duplicates": [], "unchecked": []}
    filed = filed_inside_media_root(path, media_roots, staging)
    if filed is not None:
        entry["verdict"] = "duplicate"
        entry["already_filed"] = True
        entry["duplicates"] = [{"archived_path": filed,
                                "source_id": source_id_in(filed)}]
        entry["detail"] = ("this file is the archive's own copy - it is already "
                           "filed, so there is nothing to import")
        return entry
    try:
        size = os.path.getsize(path)
    except OSError as e:
        entry["verdict"] = "indeterminate"
        entry["detail"] = "this file could not be read (%s)" % _reason(e)
        return entry
    entry["bytes"] = size
    # A file is not its own duplicate. With the already-filed check above, this
    # only fires for an archived path reached by a second name, which is a
    # repeat of one candidate rather than a missing one.
    #
    # Compared canonically, and NOT by the filesystem's (device, inode), even
    # though that is the exact identity used elsewhere. A hard link is one
    # inode with two directory entries, and one of them can be the archive's
    # filed copy: `samefile` would call the filed recording "the incoming file
    # itself", drop it from the candidate list, and clear as `new` a recording
    # the archive already holds. Excluding too little costs one wasted hash of
    # a file against itself; excluding too much costs the whole verdict.
    candidates = [c for c in by_size.get(size, [])
                  if canonical_path(c) != canonical_path(path)]
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


def mark_bundle_repeats(results, paths, cache):
    """Catch the same recording arriving twice in ONE batch.

    `check_one` compares each incoming file against the archive, which answers
    "is this already filed". It does not answer "is this the same recording as
    the file next to it", and a phone export is exactly where that happens: the
    app names one afternoon three different relative-weekday names, and the
    human hands over all three. Every one of them is honestly `new`, so the
    skill imports all three, and one recording ends up with three source records
    and its claims split between them - the same harm this script exists to
    prevent, arriving from the other direction.

    So the first of a byte-identical group keeps its `new` verdict and the rest
    become duplicates carrying `repeat_of`, the path of the one to import. Exit
    2 then means what it always means: skip the named items, import the rest.

    Only files already cleared as `new` are grouped. A file that is a duplicate
    of something filed is being skipped anyway, and one whose check could not
    finish keeps the more specific reason it has. Hashing is confined to files
    that share a byte size with another file in the same batch, and reuses this
    run's digest cache, so the common case of a batch of different recordings
    opens nothing extra at all.
    """
    by_size = {}
    for entry, path in zip(results, paths):
        if entry["verdict"] == "new" and entry.get("bytes") is not None:
            by_size.setdefault(entry["bytes"], []).append((entry, path))
    for group in by_size.values():
        if len(group) < 2:
            continue
        first_seen = {}
        for entry, path in group:
            try:
                digest = sha256_file(path, cache)
            except OSError as e:
                entry["verdict"] = "indeterminate"
                entry["detail"] = "this file could not be read (%s)" % _reason(e)
                continue
            entry["sha256"] = digest
            if digest in first_seen:
                entry["verdict"] = "duplicate"
                entry["repeat_of"] = first_seen[digest]
                entry["detail"] = ("this is the same recording as another file "
                                   "in the same batch")
            else:
                first_seen[digest] = path


def apply_archive_coverage(results, archive_unreadable):
    """Hold every verdict to what the archive side actually managed to examine.

    The one place a gap in ROOTS or ENUMERATION is turned into verdicts, so the
    rule lives somewhere instead of being re-derived at each new failure mode.
    An archived file or folder nobody could read might hold the twin of ANY
    incoming file, not of one in particular, so it cannot be attached to a single
    result: it turns every would-be `new` into an open question. `duplicate`
    stands - finding a twin does not depend on having seen the rest - and an
    entry that is already `indeterminate` keeps the more specific reason it has.

    Nothing is done here about a gap on the INCOMING side (a bundle subfolder
    that could not be listed). That gap does not make the files this run did read
    any less checked; it makes the run incomplete, which the caller reports and
    exits 3 for. Downgrading those verdicts would tell the human the wrong thing
    is uncertain.
    """
    if not archive_unreadable:
        return
    for entry in results:
        if entry["verdict"] == "new":
            entry["verdict"] = "indeterminate"
            entry["detail"] = (
                "%d archived recording(s) could not be read at all, so no "
                "recording can be cleared as new this run"
                % len(archive_unreadable))


# ---------------------------------------------------------------------------
# Refusing to write onto anything this run reads
# ---------------------------------------------------------------------------
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

    Every comparison is `could_be_same_file`, never a string equality test. The
    report is the one path here that usually does not exist yet, so half the
    question is about a file that has no inode to compare, and the other half
    is about recordings whose names the volume may fold. Both arms of that
    check lean the same way on purpose: a name this refuses that was actually
    free costs the human one more word on the command line.

    Returns the message to refuse with, or None when the path is safe to write.
    """
    if not report_path:
        return None
    for path in incoming:
        if could_be_same_file(path, report_path):
            return ("--json points at one of the recordings being checked (%s). "
                    "This check only ever reads recordings, and writing the "
                    "report there would destroy that one. Nothing was written. "
                    "Give the report a filename of its own - for example "
                    "--json %s - and run the command again."
                    % (render(path), REPORT_EXAMPLE))
    for path in archived:
        if could_be_same_file(path, report_path):
            return ("--json points at a recording already filed in the archive "
                    "(%s). This check never writes to the archive, and the "
                    "report would replace that recording. Nothing was written. "
                    "Give the report a filename of its own - for example "
                    "--json %s - and run the command again."
                    % (render(path), REPORT_EXAMPLE))
    if config_path and could_be_same_file(config_path, report_path):
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
        if _is_inside(report_path, base):
            return ("--json would write into the archive's %s folder, which "
                    "holds your filed recordings and their transcripts. This "
                    "check never writes to the archive. Nothing was written. "
                    "Put the report somewhere of your own - for example "
                    "--json %s - and run the command again."
                    % (render(base), REPORT_EXAMPLE))
    return None


def _rendered_result(entry, render):
    """One result with every path in named-root form, ready for the report.

    Rendering happens here rather than where the paths are found, so the working
    values stay real filesystem paths that can be reopened and compared, and
    exactly one layer decides how a path is spelled to the outside world.
    """
    out = dict(entry,
               duplicates=[{"archived_path": render(d["archived_path"]),
                            "source_id": d["source_id"]}
                           for d in entry["duplicates"]],
               unchecked=[{"archived_path": render(u["archived_path"]),
                           "detail": u["detail"]}
                          for u in entry["unchecked"]])
    if "repeat_of" in out:
        out["repeat_of"] = render(out["repeat_of"])
    return out


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
            # Same folder named twice (`--media-root x --media-root ./x`, or a
            # link to it) is one root; both spellings exist here, so the
            # filesystem can be asked outright.
            if any(same_file(base, existing) for _label, existing in media_roots):
                continue
            label = media_root_label(base, taken)
            taken.add(label)
            media_roots.append((label, base))
        archive_root = None
        # An explicit --media-root names a folder of filed recordings and
        # nothing else: there is no fha.yaml in play, so no inbox to exclude.
        staging = None
    else:
        archive_root = find_archive_root(args.root or os.getcwd())
        if archive_root is None:
            return fail(
                "no archive found above %s (nothing named fha.yaml). Run this from "
                "inside the archive, or pass --root <archive folder>, or check "
                "against one folder with --media-root <folder>."
                % os.path.abspath(args.root or os.getcwd()))
        try:
            media_roots, missing_roots, staging = resolve_media_roots(archive_root)
        except ConfigProblem as e:
            return fail(str(e))
        if missing_roots:
            # A configured root that is not mounted, was renamed, or was never
            # given a folder is not an empty root. Its recordings are unreadable,
            # so nothing can be cleared against it.
            return fail(
                "the archive's fha.yaml says your recordings are kept in %s, and "
                "that folder is not there right now. Recordings filed in it "
                "cannot be read, so this check cannot tell you whether your new "
                "recordings are already in the archive. Reconnect the drive, "
                "create the folder, or fix the path in fha.yaml, then run the "
                "command again."
                % ", ".join("%s (%s)" % (label, value or "no folder named")
                            for label, value in missing_roots))
        if not media_roots:
            return fail(
                "the archive at %s has no documents or photos folder to check "
                "against yet. Nothing is filed, so nothing can be a duplicate - "
                "import normally with `fha process`." % archive_root)

    incoming, incoming_unreadable, not_media = expand_inputs(args.incoming)
    if not incoming and not incoming_unreadable:
        return fail("none of those paths hold an audio or video file. Supported "
                    "extensions: %s" % ", ".join(sorted(MEDIA_EXTENSIONS)))

    named_roots = build_named_roots(media_roots, archive_root, args.incoming)

    def portable(path):
        return portable_path(path, named_roots)

    by_size, archive_unreadable = index_sizes_by_root(media_roots, staging)

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
    results = [check_one(p, by_size, cache, media_roots, staging)
               for p in incoming]
    # Where each incoming file came from, in the same named-root form as every
    # archived path. The console needs it as much as the JSON does: two bundles
    # in one run can both hold a "New Recording 4.m4a".
    for entry, src in zip(results, incoming):
        entry["path"] = portable(src)

    mark_bundle_repeats(results, incoming, cache)
    apply_archive_coverage(results, archive_unreadable)

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

    # Named by the human, outside what this check can answer for. Said out loud
    # rather than dropped: silence about a file he typed himself reads as
    # approval, which is the same failure as a wrong "new" wearing quieter
    # clothes. It does not change the exit code - it is not a recording, and the
    # skill imports recordings.
    not_checked = ["not an audio or video file, so nothing was checked for it: "
                   "%s" % portable(path) for path in not_media]

    if args.json:
        payload = {
            "tool": TOOL_NAME,
            "version": TOOL_VERSION,
            "media_roots": [label for label, _root in media_roots],
            "checked": len(results),
            "duplicates": len(duplicates),
            "indeterminate": len(indeterminate),
            "complete": not indeterminate and not incomplete,
            "results": [_rendered_result(r, portable) for r in results],
            "could_not_be_read": incomplete,
            "not_checked": not_checked,
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
            if r.get("already_filed"):
                sid = r["duplicates"][0]["source_id"]
                print("DUPLICATE  %s is already filed in the archive%s - "
                      "nothing to import"
                      % (r["path"], " (%s)" % sid if sid else ""))
            elif r.get("repeat_of"):
                print("DUPLICATE  %s is byte-identical to %s in the same batch "
                      "- import one of them, not both"
                      % (r["path"], portable(r["repeat_of"])))
            elif r["verdict"] == "duplicate":
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
        for path in not_media:
            print("SKIPPED    %s - not an audio or video file, so nothing was "
                  "checked for it" % portable(path))
        if duplicates:
            print("")
            print("Do not import the duplicates: report each one with the path of "
                  "the recording it repeats and leave the bundle untouched. Confirm "
                  "the twin's record with `fha find <S-id>`. If the bundle carries a "
                  "transcript the archive lacks, that is an attach onto the existing "
                  "source with `fha process <filed-primary> --more <file> <role>`, "
                  "not a second import. Where the twin is another file in the same "
                  "batch, import the one named on the line and skip the other - one "
                  "sitting is one source record.")
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
