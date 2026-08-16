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

WHAT IT NEVER DOES
==================
Read-only, both sides. It opens files to hash them and nothing else: no rename,
no move, no delete, no write to any archived file. It reports; the human and
`fha process` decide. It also never claims the twin's S-id is correct beyond
what the filename says - `fha find <S-id>` is how you confirm the record.

WHERE IT LOOKS
==============
The archive root is the folder holding `fha.yaml`, found by walking up from
`--root` (or from the current directory). The media roots come from that file's
`roots:` mapping, because `documents:` and `photos:` are allowed to point outside
the archive entirely - hardcoding `<archive>/documents` silently finds nothing on
those archives. `--media-root` overrides the lot when you want to check against
one folder.

Exit codes
==========
  0  no incoming file matched anything already archived - safe to import
  2  at least one incoming file is byte-identical to a filed recording; the
     skill's rule is to report that item and skip it, never to import it twice
  1  usage or IO error (bad path, unreadable archive root)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

TOOL_NAME = "find_duplicate_media.py"
TOOL_VERSION = "1.0.0"

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


def fail(msg):
    """One-line plain error on stderr, always naming what to do next."""
    sys.stderr.write("%s: error: %s\n" % (TOOL_NAME, msg))
    return 1


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
    """The `roots:` mapping out of fha.yaml, or {} when it cannot be read.

    PyYAML is the archive tooling's own dependency, so it is normally present.
    When it is not, a deliberately small line reader picks the two-space
    `alias: value` entries under `roots:` out of the file. That fallback only
    has to survive the shape the template writes; anything more exotic falls
    through to the built-in defaults, which is the safe direction to fail.
    """
    path = os.path.join(archive_root, "fha.yaml")
    if not os.path.isfile(path):
        return {}
    try:
        text = open(path, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        return {}
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            data = yaml.safe_load(text) or {}
        except Exception:
            data = {}
        roots = data.get("roots") if isinstance(data, dict) else None
        if isinstance(roots, dict):
            return {str(k): str(v) for k, v in roots.items() if v}
        return {}
    roots = {}
    in_roots = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[:1].isspace():
            in_roots = line.split("#", 1)[0].strip() == "roots:"
            continue
        if in_roots and ":" in line:
            key, _sep, value = line.strip().partition(":")
            value = value.split("#", 1)[0].strip().strip("'\"")
            if value:
                roots[key.strip()] = value
    return roots


def resolve_media_roots(archive_root):
    """Absolute paths of every root that could already hold this recording.

    Resolved through `roots:` rather than joined onto the archive root, because
    an external documents root is normal (AGENTS.md: "asset roots may live
    OUTSIDE this folder"). A hardcoded `<archive>/documents` on such an archive
    finds zero files and reports every incoming recording as new - the exact
    failure this script exists to prevent.
    """
    roots = _roots_from_config(archive_root)
    out = []
    for alias in MEDIA_ROOT_ALIASES:
        value = roots.get(alias, alias)
        base = value if os.path.isabs(value) else os.path.join(archive_root, value)
        base = os.path.abspath(base)
        if os.path.isdir(base) and base not in out:
            out.append(base)
    return out


# ---------------------------------------------------------------------------
# Size index and hashing
# ---------------------------------------------------------------------------
def is_media(path):
    return os.path.splitext(path)[1].lower() in MEDIA_EXTENSIONS


def index_sizes_by_root(media_roots):
    """Map byte size -> [archived media paths of that size].

    Sizes come from `os.scandir`'s stat, which the directory walk already has,
    so building this index opens no files at all. It is the cheap half of the
    check; hashing only happens for the sizes that actually collide.
    """
    by_size = {}
    for root in media_roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if not is_media(name):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue          # vanished or unreadable: not a twin we can prove
                by_size.setdefault(size, []).append(full)
    return by_size


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
    """Flatten files and folders into the media files to check, sorted."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
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
    return unique


def check_one(path, by_size, cache):
    """Result dict for one incoming file: duplicate, new, or unreadable."""
    entry = {"file": os.path.basename(path), "verdict": "new", "duplicates": []}
    try:
        size = os.path.getsize(path)
    except OSError as e:
        entry["verdict"] = "unreadable"
        entry["detail"] = str(e)
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
        entry["verdict"] = "unreadable"
        entry["detail"] = str(e)
        return entry
    entry["sha256"] = digest
    for cand in candidates:
        try:
            if sha256_file(cand, cache) != digest:
                continue
        except OSError:
            continue
        entry["duplicates"].append({
            "archived_path": cand,
            "source_id": source_id_in(cand),
        })
    if entry["duplicates"]:
        entry["verdict"] = "duplicate"
    return entry


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
                   help="also write the findings as JSON")
    p.add_argument("--quiet", action="store_true", help="suppress the stdout summary")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    for p in args.incoming:
        if not os.path.exists(p):
            return fail("not found: %s - check the path and run the command again" % p)

    if args.media_roots:
        media_roots = []
        for d in args.media_roots:
            if not os.path.isdir(d):
                return fail("--media-root is not a folder: %s - point it at the "
                            "folder holding the archived recordings" % d)
            media_roots.append(os.path.abspath(d))
        archive_root = None
    else:
        archive_root = find_archive_root(args.root or os.getcwd())
        if archive_root is None:
            return fail(
                "no archive found above %s (nothing named fha.yaml). Run this from "
                "inside the archive, or pass --root <archive folder>, or check "
                "against one folder with --media-root <folder>."
                % os.path.abspath(args.root or os.getcwd()))
        media_roots = resolve_media_roots(archive_root)
        if not media_roots:
            return fail(
                "the archive at %s has no documents or photos folder to check "
                "against yet. Nothing is filed, so nothing can be a duplicate - "
                "import normally with `fha process`." % archive_root)

    incoming = expand_inputs(args.incoming)
    if not incoming:
        return fail("none of those paths hold an audio or video file. Supported "
                    "extensions: %s" % ", ".join(sorted(MEDIA_EXTENSIONS)))

    by_size = index_sizes_by_root(media_roots)
    cache = {}
    results = [check_one(p, by_size, cache) for p in incoming]

    duplicates = [r for r in results if r["verdict"] == "duplicate"]
    unreadable = [r for r in results if r["verdict"] == "unreadable"]

    # Archived paths are reported so the human can open the twin, but they are
    # this machine's absolute paths. Anything written to disk gets them relative
    # to the archive root instead (AGENTS_TOOLING.md §11: a local absolute path
    # must not end up in a committed or archived file).
    def portable(path):
        if archive_root:
            try:
                return os.path.relpath(path, archive_root).replace("\\", "/")
            except ValueError:
                pass
        return os.path.basename(path)

    if args.json:
        payload = {
            "tool": TOOL_NAME,
            "version": TOOL_VERSION,
            "media_roots": [portable(r) for r in media_roots],
            "checked": len(results),
            "duplicates": len(duplicates),
            "results": [
                dict(r, duplicates=[{"archived_path": portable(d["archived_path"]),
                                     "source_id": d["source_id"]}
                                    for d in r["duplicates"]])
                for r in results
            ],
            "paths_note": "paths are relative to the archive root; absolute "
                          "machine paths are deliberately not recorded",
        }
        try:
            parent = os.path.dirname(os.path.abspath(args.json))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            tmp = "%s.tmp-%d" % (os.path.abspath(args.json), os.getpid())
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, indent=2) + "\n")
            os.replace(tmp, args.json)
        except OSError as e:
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
                          % (r["file"], portable(d["archived_path"]),
                             " (%s)" % sid if sid else ""))
            elif r["verdict"] == "unreadable":
                print("SKIPPED    %s could not be read: %s"
                      % (r["file"], r.get("detail", "unknown reason")))
            else:
                print("new        %s" % r["file"])
        if duplicates:
            print("")
            print("Do not import the duplicates: report each one with the path of "
                  "the recording it repeats and leave the bundle untouched. Confirm "
                  "the twin's record with `fha find <S-id>`. If the bundle carries a "
                  "transcript the archive lacks, that is an attach onto the existing "
                  "source with `fha process <filed-primary> --more <file> <role>`, "
                  "not a second import.")
    if unreadable:
        sys.stderr.write("%s: warning: %d file(s) could not be read and were "
                         "neither cleared nor flagged\n" % (TOOL_NAME, len(unreadable)))
    return 2 if duplicates else 0


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
