#!/usr/bin/env python3
"""
packet.py - fha packet: build a person data-export packet.

  fha packet <P-id> [-o out/] [--include-research] [--include-restricted]
                     [--include-dna] [--no-photos] [--dry-run] [--overwrite]
                     [--root PATH]

ARCHITECTURE OVERVIEW
----------------------
A packet is a private, family-facing export of everything the archive knows
about one curated person: their profile, a freshly generated timeline,
every source that cites them, those sources' asset files, and (unless
suppressed) every photo of them the photo index can find. It is gathered as
**copies** into a working directory, then zipped (TOOLING §8). The archive
itself is never touched.

This is explicitly NOT the public/standalone export path (`fha site
--standalone`, TOOLING §12) - the packet's README says so. A packet may
include `living: false` people's full prose and cite other people who are
still living, with a caution in the README. The packet subject is different:
SPEC §21 binds person packets to their own subject rule (separate from the
public-output redaction rules `fha site` follows), so `living: true` and
`living: unknown` subjects are refused unless a future SPEC/TOOLING change
adds an explicit packet opt-in.

PRIVACY RULES (TOOLING §8 - apply at gather time, not as a post-filter):
  - `living: unknown` is treated the same as `living: true`.
  - The packet subject must be `living: false`; packets for living/unknown
    subjects are refused before any output directory is created. A restricted
    subject is refused too - absolutely for `restricted: by-request`, otherwise
    unless --include-restricted (or --include-dna for `restricted: dna`).
  - The `restricted` marker (SPEC §19) is honored wherever it appears - a
    source, a single claim, or the subject. The value is read from the record
    file, not just the index's 0/1, so a free-text type is recognized. Plain
    restrictions open with --include-restricted; `restricted: dna` needs
    --include-dna (DNA is always restricted, lint E017); `restricted: by-request`
    never opens under any flag.
  - A MARKER THAT COULD NOT BE READ IS TREATED AS RESTRICTED, under every flag
    (`_record_restriction`). Reading the value from the file is what makes the
    free-text types work, and it is also what makes a failed read look exactly
    like a person or a source that carries no marker at all. The subject is
    refused (`restricted-subject`) and an unreadable source is excluded with its
    files, both saying which file and how to repair it. Under every flag,
    because `restricted: by-request` is the thing that cannot be ruled out and
    it is the one type no flag opens. Note what does NOT raise here:
    `_lib.read_record` reports a gone file, a permission error and malformed
    YAML as an E010 entry in `parse_errors` rather than an exception, and a
    record with no frontmatter block at all produces neither - just an empty
    `meta` that reads as "no marker".
  - A restricted claim inside an otherwise-included source is withheld from the
    timeline AND cut from the copied source record
    itself (the README counts what was left out, in plain words) - the withhold
    never requires the claim to carry an `id:`, because id-less claims are a
    valid hand-authored state. The profile copy likewise drops withheld
    `name_variants` entries and their `aliases:` mirrors, matching mirrors
    through wikilink wrappers and the nested list an unquoted `[[name]]`
    YAML-parses to, the same forgiving forms every alias consumer resolves.
    A record that cannot be redacted safely - or whose claims cannot even be
    read - is never shipped verbatim: a source is left out of the packet with
    a warning (and its indexed claims are kept off the timeline), and a
    profile (which the packet cannot ship without) fails the build.
  - Unaccepted AI-draft prose (`<!-- AI-DRAFT ... -->`, the AGENTS.md AI-pass
    contract) is withheld from the profile copy. No packet flag opens it -
    the flags govern `restricted`, a different promise; acceptance is
    `fha confirm draft`. A damaged marker fails the build (draft can no
    longer be told from accepted prose, and the centerpiece cannot ship
    verbatim). Research copies stay byte copies by the documented round-2
    scope decision; one carrying a draft marker gets a README caution line.
  - #75's visible purpose block, and (for a profile) #76's `## Sources`
    region, never reach a shipped copy either (`_strip_scaffolding_blocks`) -
    they are instructions for whoever works in the *archive*, and the
    `## Sources` region names every source that touches this person by
    archive-relative path without the packet's own privacy filtering, so it
    is dropped rather than shipped verbatim. Nothing replaces it: the
    included source records/files carry the same information already
    privacy-filtered. A §16 section nobody has written yet goes with them
    (#125): "Write their story in plain sentences..." under `## Biography`
    is the template addressing the archive owner, and a relative reading the
    packet has no way to tell it from what the family actually wrote.
  - Excluded sources are still named (ID + reason, no title) in the README so
    the human knows material exists but was withheld, not silently dropped -
    and the reason distinguishes restricted / DNA / could-not-be-read, because
    an absence reported under the wrong cause sends him looking in the wrong
    place.
  - Any *other* person named in the packet's included claims/sources who is
    themselves `living`/`unknown` gets a README caution (their prose/facts
    are still included - packets are private, not for redistribution).

ALL THERE OR NOT AT ALL: the finished folder is walked with an error seam
before it is zipped (`_zip_directory`). `os.walk` and `rglob` both read a
folder they cannot list as an empty one, so a zip could go out missing the
very source a claim rests on while the run said "packet written" - and the
packet is handed to a relative who has no way to check it against the
archive. A folder that will not open therefore fails the build onto the
write-failed arm, which clears the half-built folder and names the cause.

PHOTO GATHERING (TOOLING §8's "all photos of grandma" union):
  (a) photos carrying the bare P-id keyword           - photo_people via='pid-keyword'
  (b) photos whose face-region tags matched exactly    - photo_people via='face-tag'
  (c) photos matched by name/name_variants (unverified) - photo_people via='name-match'
  (d) image files attached to the *included* sources (whether or not the
      photoindex separately resolved them to this person)
  `photo_people` already computes the union of (a)-(c) per photoindex.py's
  `_resolve_photo_people`; this tool only adds (d) and then expands every
  matched path to its full variation group (front+back+crop, etc.) via
  `photo_groups`/`photos.group_id` so a person's photo entry never ships
  the front scan without its back.
  A photo the catalog still knows but disk no longer has (reconcile's
  'MISSING:' key) is never copied - a packet is a physical bundle - but it
  IS named in the README's missing-files list and warned about with the
  command that re-links it. Its tags and its unverified-name-match status
  still count, because they describe the physical photo rather than the one
  scan of it: whenever a live variant of that photo ships, the vanished
  side's living-person caution and "matched by name only" caution ship with
  it. Both cautions are computed from the files actually copied, so a group
  that contributed nothing to the bundle contributes no cautions either.

WHY A LIBRARY FUNCTION (`run_packet`): mirrors the xref/cooccur/report
convention of a testable `run_*(archive_root, ...) -> dict` core, separate
from the CLI handler that turns the dict into exit codes and stdout text.

CODE MAP
--------
  The `restricted` marker
    _restricted_type              - a raw `restricted:` value → its type, or None
    _restricted_included          - does a record carrying this value belong in the export?
    _record_restriction           - one record's own marker, or why it could NOT be read
                                     (the four ways that happens, and why a failed read
                                     must never be taken for "no marker")

  Helpers
    _today                         - packet directory/README date stamp
    _curated_person                - lookup + curated-tier gate
    _source_ids_for_person        - claim_persons ∪ source_people union (views.py's pattern,
                                     duplicated per-tool per TOOLING §15 "tools never import tools")
    _source_restricted_value      - one source's marker, index fallback, or unreadable
    _classify_sources             - split source ids into included/excluded/unreadable by
                                     the privacy rules
    _other_named_persons          - living/unknown persons named by included sources, for the
                                     README caution
    _resolve_source_files         - source_files rows → resolved paths + missing/unresolvable notes
    _is_image_path                - extension sniff for photo-type asset files

  Privacy redaction of copied records
    (read_text_exact / write_text_exact_atomic - the newline-preserving,
                                     crash-safe IO that keeps a redacted copy
                                     byte-faithful outside the cuts - now live in
                                     _lib, shared with claims surgery)
    _yaml_list_item_spans         - map a YAML list's entries to their line spans
    _redact_source_record_text    - cut the flag-withheld claims from a source record copy
                                     (decided per parsed entry, never by claim id)
    _redact_source_record_files_field - rewrite a non-portable files: entry's own
                                     path (the same leak _redact_asset_path closes in
                                     README.txt, but inside the copied record itself)
    _strip_frontmatter_list_entries - surgical removal from a top-level frontmatter list
    _flatten_alias_strings        - strings inside a nested-list alias entry
    _redact_profile_text          - drop withheld name variants + their alias mirrors
    _strip_profile_drafts         - withhold unaccepted AI-draft prose from the profile copy
    _strip_scaffolding_blocks     - drop #75's purpose block (+ #76's `## Sources`
                                     region and #125's never-written §16 sections,
                                     for a profile) before a copy ships
    _source_copy_plan             - per-source copy mode (byte/redact/unsafe) + timeline excludes

  Photo gathering
    _live_alias                   - the real path under a reconcile 'MISSING:' key
    _is_missing_key               - is this catalog key a photo that is not on disk?
    _photo_people_paths           - photo_people rows for this pid (a/b/c union, already resolved)
    _expand_photo_groups          - path set → full variation-group path set
    _name_only_group_aliases      - paths whose whole photo group is an unverified name match
    _source_image_paths           - image-suffixed files among included sources' assets (d)

  Timeline
    _build_timeline_text          - self-contained fresh timeline.md content, filtered to the
                                     packet's included sources (no GENERATED header - this is an
                                     export copy, not a tracked archive view file)

  Packaging
    _unique_dest_path             - collision-safe copy destination inside a packet subdirectory
    _copy_into                    - copy one file, returning the dest path or None on a missing src
    _plural_note                  - one plain "left out for privacy" README line
    _draft_note                   - one plain "draft awaiting your review left out" README line
    _copy_redacted_source         - like _copy_into, but with the withheld claims cut out
    _copy_source_with_scaffolding_stripped - like _copy_into, but with #75's purpose
                                     block cut out (the ordinary-source sibling of
                                     _copy_redacted_source, for when no claim needs cutting)
    _write_readme                 - manifest + disclaimer + privacy captions
    _zip_directory                - zip the finished packet directory

  Core / CLI
    _display_path                 - print paths relative to archive when possible
    run_packet                    - library entry point: gather, copy, write, zip
    _cmd_packet, register, _standalone_main
"""

from __future__ import annotations

import argparse
import datetime
import re
import shutil
import sqlite3
import sys
import unicodedata
import zipfile
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).parent))

import yaml

from _lib import (
    # _AI_DRAFT_MARK_RE is _lib-private on purpose (the marker grammar has ONE
    # home, kept in sync with confirm.py's flip grammar); packet imports it for
    # the README draft count only, so the count can never drift from what
    # strip_unaccepted_drafts actually cut. The strip itself is the public API.
    _AI_DRAFT_MARK_RE,
    CLAIMS_RE,
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FRONT_RE,
    FhaConfigError,
    Result,
    configure_utf8_stdout,
    fmt_id_display,
    is_placeholder_name,
    is_working_copy,
    load_fha_yaml,
    normalize_id,
    open_index_db,
    path_to_alias,
    photoindex_status,
    read_record,
    read_text_exact,
    resolve_path,
    resolve_root_arg,
    strip_generational_suffix,
    strip_link_wrapper,
    strip_unaccepted_drafts,
    strip_unfilled_person_sections,
    unreadable_dir_recorder,
    walk_files,
    write_text_exact_atomic,
    yaml_inline,
)

configure_utf8_stdout()


# ── The `restricted` marker (SPEC §19, TOOLING §1) ─────────────────────────────
# `restricted` may sit on a source, a claim, a person, or a name. It defaults to
# the boolean `true` but may carry a free-text type instead; two types never
# open under any flag. These helpers are duplicated per export tool (tools never
# import tools, TOOLING §15) and agree exactly on the contract.

def _restricted_type(value) -> str | None:
    """Normalize a raw `restricted:` value to its type, or None when unrestricted.

    `read_record` coerces booleans to the strings `'true'`/`'false'`, so the
    plain boolean arrives as `'true'`. An absent/false value is unrestricted; a
    plain truthy value is the type `'plain'`; any other string is its own type
    (`'dna'`, `'by-request'`, `'deadname'`, …), lowercased."""
    if value in (None, False, '', 'false'):
        return None
    if value in (True, 'true'):
        return 'plain'
    return str(value).strip().lower() or 'plain'


def _restricted_included(value, *, include_restricted: bool, include_dna: bool) -> bool:
    """Does a record carrying this `restricted:` value belong in the export?

    Unrestricted material is always included. `dna` opens only with
    `--include-dna`; `by-request` never opens under any flag; every other type
    (and the plain boolean) opens only with `--include-restricted`. Public paths
    pass both flags False, so anything restricted is excluded.

    This answers a question about a VALUE, so it has no way to tell a record
    that said nothing from a record nobody could read - both arrive as None and
    both read as "include". Every caller that gets its value by reading a file
    must therefore ask `_record_restriction` first and handle its `trouble`
    before consulting this function."""
    rtype = _restricted_type(value)
    if rtype is None:
        return True
    if rtype == 'dna':
        return include_dna
    if rtype == 'by-request':
        return False
    return include_restricted


def _record_restriction(path: Path) -> tuple[object, str | None]:
    """Read one record's own `restricted:` marker. Returns (value, trouble).

    `trouble` is None when the marker was genuinely read (the value may still
    be None - that is a record saying it carries no restriction). A non-None
    `trouble` is a plain phrase saying why the marker could NOT be read, and
    every caller must treat that as "restricted", never as "no marker": a
    missing privacy marker is indistinguishable from a person who never asked
    to be left out, and only one of those two readings is safe to export.
    This is the same withhold `fha site` makes for the same reason (site.py,
    `_load_restriction_markers`).

    FOUR ROUTES END HERE, and knowing why matters more than the code does,
    because guarding only the obvious one leaves the real ones open:

      1. The file cannot be opened or decoded - gone, locked, not UTF-8.
      2. The frontmatter is not valid YAML.
      3. The record has no frontmatter block at all. Nothing raises and nothing
         is reported: `FRONT_RE` simply does not match, so the marker reads as
         absent. This is the shape that shipped a written packet for a
         `restricted: by-request` person.
      4. The frontmatter parses to something that is not a block of fields (a
         bare scalar, a list), so there is no key to look up.

    WHY THIS PARSES THE FRONTMATTER ITSELF rather than asking `read_record` and
    checking `parse_errors`, which is the obvious spelling and is wrong in both
    directions. Too narrow: `read_record` does not RAISE for routes 1 and 2 - a
    gone file, a permission error and malformed YAML all come back as an E010
    entry with `meta` empty - so an `except` arm around it catches almost
    nothing, which is exactly how the bug this replaces survived. Too broad:
    its `parse_errors` also carries CLAIMS-block failures, and a source whose
    claims YAML will not parse has a perfectly readable frontmatter marker.
    Escalating that into a source-level exclusion would take the record's asset
    files down with it, when the claim-level guard already answers that
    question correctly and more precisely (`_source_copy_plan` withholds the
    record and its claims, and lets the assets ship). This function asks one
    question - can the record's own `restricted:` value be read - and the
    frontmatter is the whole of where that value can live.

    Route 3 is where the line gets drawn, so draw it explicitly: a frontmatter
    block that PARSES - even one whose only line is blank, so it parses to no
    fields at all - is the record STATING that it carries no restriction, and
    is honored as such. No block is a record with nowhere to put the marker,
    which is damage rather than a statement. The test of which one this is is
    `FRONT_RE`, the same reader every other tool uses, so a file the archive
    treats as having no frontmatter is treated that way here too rather than
    getting a second opinion from this function.

    Refusing route 3 costs nothing a correct archive would have wanted: `tier:`
    and `living:` live in that same frontmatter, so a person whose block is
    gone indexes as a non-living stub on the next `fha index` and could not be
    a packet subject anyway. The only exports it stops are the ones where the
    index and the file already disagree.

    The raw parsed value is returned uncoerced, and the marker helpers here are
    built for that: `_restricted_type` matches the YAML booleans `True`/`False`
    and `read_record`'s coerced `'true'`/`'false'` strings alike, so the two
    readers agree on every value the marker can hold."""
    try:
        text = read_text_exact(path)
    except (OSError, UnicodeError) as e:
        return None, f'the file could not be read ({e})'
    fm = FRONT_RE.match(text)
    if fm is None:
        return None, (
            'the record has no frontmatter block, which is the only place a '
            'privacy marker can live'
        )
    try:
        meta = yaml.safe_load(fm.group(1))
    except yaml.YAMLError:
        return None, 'the frontmatter is not valid YAML'
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        return None, 'the frontmatter did not read as a block of fields'
    return meta.get('restricted'), None


# ── Privacy redaction of copied records ────────────────────────────────────────
# A packet ships COPIES of record files, and a copy can leak what the gather
# filters withheld: a restricted claim's YAML inside an otherwise-included
# source, or a restricted `name_variants` entry (a private prior name) in the
# profile's frontmatter. These helpers cut exactly those entries out of the
# copy - a surgical line-span removal, so the rest of the file stays
# byte-faithful - and every doubt fails CLOSED: a copy that cannot be redacted
# is not written at all.
# The newline-preserving IO pair these cuts depend on (read_text_exact /
# write_text_exact_atomic) moved to _lib so the claims-surgery tools share the cure.

def _yaml_list_item_spans(block: str) -> list[tuple[int, int]] | None:
    """Offsets of each top-level `- ` entry in a YAML list block.

    Line surgery has to know exactly which characters belong to which entry.
    An entry starts at a bullet line at the list's own indent (the first
    bullet's indent) and owns every following line - continuations, nested
    lists, blanks, comments - until the next such bullet. Returns None when
    non-comment content precedes the first bullet: the block is then not a
    plain list, and cutting lines out of it would not be safe."""
    spans: list[list[int]] = []
    bullet_indent: int | None = None
    pos = 0
    for line in block.splitlines(keepends=True):
        end = pos + len(line)
        stripped = line.lstrip(' ')
        indent = len(line) - len(stripped)
        content = stripped.rstrip('\r\n')
        is_bullet = content == '-' or content.startswith('- ')
        if is_bullet and (bullet_indent is None or indent == bullet_indent):
            bullet_indent = indent
            spans.append([pos, end])
        elif spans:
            spans[-1][1] = end
        elif content and not content.startswith('#'):
            return None
        pos = end
    return [(s, e) for s, e in spans]


def _redact_source_record_text(
    text: str, *, include_restricted: bool, include_dna: bool,
) -> tuple[str, int] | None:
    """Cut every claim entry the flags withhold out of a source record's fenced
    `## Claims` block, leaving every other character untouched.

    Surgery instead of re-serializing because the copy should stay recognizably
    the human's own file. Withheld-ness is decided HERE, per parsed entry, on
    the very parse that maps entries to their line spans - claim ids play no
    part. Round-2 finding 1: the previous design collected withheld C-ids up
    front and cut by id, so a restricted claim with no `id:` (a lint-blessed
    state - the quickstart teaches id-less claims) never entered the set and
    shipped verbatim. One parse for both decision and cut also removes the old
    two-read race where the id set and the splice could disagree.

    Any doubt returns None so the caller fails CLOSED - the record is left out
    of the packet, never shipped verbatim: no fenced block (an unfenced Claims
    section still parses through read_record, but line surgery on it is not
    safe), unparseable YAML, a non-list block, bullet spans that do not align
    with the parsed entries, or an entry that is not a mapping (a stray prose
    bullet cannot even be checked for a `restricted:` marker). An empty `- `
    bullet parses to None and is kept: it has no content to withhold.

    Returns (new_text, claims_removed). An emptied block stays a valid record:
    a bare ```` ```yaml ``` ```` fence parses as an empty claims list."""
    fm = FRONT_RE.match(text)
    body_start = fm.end() if fm else 0
    m = CLAIMS_RE.search(text[body_start:])
    if not m:
        return None
    start = body_start + m.start(1)
    end = body_start + m.end(1)
    block = text[start:end]
    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError:
        return None
    if parsed is None:
        parsed = []
    if not isinstance(parsed, list):
        return None
    spans = _yaml_list_item_spans(block)
    if spans is None or len(spans) != len(parsed):
        return None
    remove: list[tuple[int, int]] = []
    for item, span in zip(parsed, spans):
        if item is None:
            continue
        if not isinstance(item, dict):
            return None
        if not _restricted_included(
            item.get('restricted'),
            include_restricted=include_restricted, include_dna=include_dna,
        ):
            remove.append(span)
    if not remove:
        return text, 0
    for s, e in reversed(remove):
        block = block[:s] + block[e:]
    return text[:start] + block + text[end:], len(remove)


# A source record's `files:` entry always opens with `file:` as its very
# first key, at the bullet line itself (`  - file: <alias>`, what
# `process.py`'s `_render_scaffold_file_entry` always writes) - but a hand
# edit is free to reorder an entry's keys onto later lines, so this matches
# a `file:` key at EITHER position: the bullet line (`-\s*file\s*:`) or a
# plain indented continuation line (`\s*file\s*:`). Used only inside a
# single already-located entry span (`_redact_frontmatter_files_field`
# below), never across a whole record, so a `file:` line belonging to a
# DIFFERENT entry is never in scope to be matched by mistake.
_FILE_KEY_LINE_RE = re.compile(r'(?m)^([ \t]*(?:-[ \t]+)?file[ \t]*:[ \t]*)([^\r\n]*)(\r?\n|$)')

# A YAML literal (`file: |`) or folded (`file: >`) block scalar puts its
# real value on the FOLLOWING indented lines, not on the `file:` line
# itself - `yaml.safe_load` joins those lines into the parsed value just
# fine, but this line-surgery function only ever rewrites the ONE line
# `_FILE_KEY_LINE_RE` matched. Recognizing the indicator here lets
# `_rewrite_file_key_in_entry` refuse the rewrite instead of silently
# leaving the continuation lines - and the raw path they still hold -
# untouched (issue #170 finding 1, round-10 audit: the fourth bypass a
# single post-merge review pass found after six already-patched rounds on
# this redaction surface). An optional explicit indentation indicator
# digit and/or chomping indicator (`|-`, `|2+`, …) and a trailing comment
# are all valid YAML here, so all three are allowed - and (round-11 audit,
# post-merge Codex review of #197, finding 1) EITHER ORDER of the two
# modifiers is valid YAML too (`|2-` and `|-2` are both the same literal
# block scalar, indentation-2 chomp-strip): the previous pattern only
# accepted chomping-then-digit and silently fell through to treating
# `|2-` as an ordinary one-line scalar VALUE instead - meaning
# `_rewrite_file_key_in_entry` didn't refuse the rewrite at all, it
# "succeeded", replacing the `file: |2-` header with the redacted value
# while leaving the real path sitting untouched on the continuation line
# right below it. Matching both orders explicitly closes that gap; every
# case this used to catch is still caught (the two orders are simply two
# branches of the same alternation).
_BLOCK_SCALAR_VALUE_RE = re.compile(r'^[|>](?:[+-]?\d?|\d?[+-]?)\s*(#.*)?$')


def _rewrite_file_key_in_entry(entry_text: str, new_value: str) -> tuple[str, int]:
    """Replace the VALUE on one `files:` entry's own `file:` line with
    `new_value`, wherever that line sits inside `entry_text` - every other
    character in the entry (role, comments, indentation, line endings)
    is untouched. Returns (new_entry_text, substitutions_made); a caller
    that gets back anything other than exactly 1 must not trust the
    result (issue #170 finding 1, P1 follow-up - post-merge Codex review
    of #176, round-9 audit).

    Refuses the rewrite - returning `(entry_text, 0)`, the same "could not
    trust it" signal the caller already treats as fail-closed - in two
    cases neither of which a caller should ever try to paper over with a
    partial edit: more than one line in this entry matches `file:` shaped
    syntax at all (ambiguous - which one is the real key is not this
    function's call to make), or the matched line's value is a YAML block
    scalar indicator (`_BLOCK_SCALAR_VALUE_RE`, round-10 audit) - the real
    value then lives on continuation lines this function does not attempt
    to locate and cut. Both read as "cannot safely rewrite this line", not
    "nothing to rewrite" - it is the CALLER's job (`_redact_frontmatter_
    files_field`) to fail the whole record closed on a 0, never to assume
    a 0 means the entry was already clean."""
    matches = list(_FILE_KEY_LINE_RE.finditer(entry_text))
    if len(matches) != 1:
        return entry_text, 0
    m = matches[0]
    if _BLOCK_SCALAR_VALUE_RE.match(m.group(2).strip()):
        return entry_text, 0
    new_text = entry_text[:m.start()] + m.group(1) + yaml_inline(new_value) + m.group(3) + entry_text[m.end():]
    return new_text, 1


def _redact_frontmatter_files_field(fm_text: str) -> tuple[str, int] | None:
    """Redact any non-portable `file:` value inside a `files:` frontmatter
    list, in place - the companion to `_strip_frontmatter_list_entries`,
    but REWRITING one field within each entry instead of removing the
    entry outright (a `files:` entry also carries `role:`/`copy:`/
    `is_primary:`/`original_filename:` the packet still needs - dropping
    the whole entry would silently un-list an asset the packet actually
    ships in files/).

    This is the fix for issue #170 finding 1's other half (P1, post-merge
    Codex review of #176, round-9 audit): `_redact_asset_path` already hid
    a foreign `files:` entry's raw path from the missing-asset SENTENCE in
    README.txt, but the packet build separately copies each included
    source's own RECORD into the exported packet's `sources/` folder
    (`_copy_redacted_source`/`_copy_source_with_scaffolding_stripped`), and
    neither of those touched the record's own frontmatter `files:` field -
    so a `files:` entry naming a real local path off the archive owner's
    machine (`C:\\Users\\andrew\\Secret Folder\\private-scan.tif`) still
    shipped verbatim inside the exported packet's copied source record,
    disclosing the owner's username and drive layout to whoever the packet
    is handed to, even though the SAME entry's mention in README.txt was
    already correctly redacted. This function is `_redact_asset_path`
    applied to the OTHER surface: every included source's own copy of its
    `files:` list, not just the README's summary of it.

    Handles the same two shapes `_strip_frontmatter_list_entries` does: a
    block list under `files:` (what `process.py`'s
    `_render_scaffold_file_entry` always writes) and an inline flow list
    (`files: [{file: ..., role: ...}]` - legal YAML nothing here ever
    generates, but a hand edit could).

    Returns (new_fm_text, redacted_count); (fm_text, 0) when the key is
    absent (or present with an explicitly empty/null value - `files:` with
    nothing after it and no indented list under it) or holds a list but no
    entry's `file` value is foreign. Returns None - the caller's fail-
    closed signal - in three cases: the key is present but its value is
    not a list at all (round-10 audit, issue #170 finding 1: a hand edit
    like `files: {file: /Users/alice/private.pdf}`, a mapping rather than
    the expected list, used to read identically to "no files: key" and
    pass straight through unredacted - a PRESENT malformed value can never
    be proven clean the way a genuinely absent key can); at least one
    entry's `file` value IS foreign and cannot be safely mapped back to
    its own line (a multiline block-scalar `file:` value is one such case,
    round-10 audit - see `_rewrite_file_key_in_entry`); or the bullet spans
    don't line up with the parsed entries at all. A missing record in a
    packet is recoverable, a
    leaked local path is not (the same posture `_redact_source_record_text`
    already takes for an unparseable Claims block).

    The key line is matched as EITHER bare `files:` or quoted `"files":`/
    `'files':` (round-11 audit, post-merge Codex review of #197, finding
    4): `yaml.safe_load` resolves a quoted scalar key to the identical
    plain string `'files'`, so a hand edit that quotes the key changes
    nothing about how the record actually parses - but this function finds
    the key line itself by textual search BEFORE handing anything to
    PyYAML, and the old search only ever looked for the bare spelling. A
    quoted key therefore looked like "no `files:` key present at all",
    which reads identically to the genuinely-absent case above and passed
    the whole entry through uninspected, non-portable path included."""
    key_re = re.compile(r'^(?:"files"|\'files\'|files)\s*:\s*(.*)$')
    lines = fm_text.splitlines(keepends=True)
    key_idx = None
    key_start = 0
    inline_rest = ''
    offset = 0
    for i, line in enumerate(lines):
        if not line.startswith((' ', '\t')):
            m = key_re.match(line.rstrip('\r\n'))
            if m:
                key_idx = i
                key_start = offset
                inline_rest = m.group(1).strip()
                break
        offset += len(line)
    if key_idx is None:
        return fm_text, 0
    key_line = lines[key_idx]
    key_end = key_start + len(key_line)

    if inline_rest and not inline_rest.startswith('#'):
        # Inline flow form: the whole list lives on the key line, so it is
        # simplest to re-dump the whole line (same shape
        # `_strip_frontmatter_list_entries` uses for its own inline arm).
        try:
            doc = yaml.safe_load(key_line)
        except yaml.YAMLError:
            return None
        value = doc.get('files') if isinstance(doc, dict) else None
        if value is None:
            return fm_text, 0
        if not isinstance(value, list):
            # A `files:` key IS present here but its value is not the list
            # shape every reader downstream expects (a mapping, a bare
            # string, a number, …) - round-10 audit, issue #170 finding 1:
            # the OLD check here read this identically to "no files: key
            # at all" and returned the frontmatter completely unchanged,
            # so a hand edit like `files: {file: /Users/alice/private.pdf}`
            # shipped its raw path straight through, unredacted, in both
            # README.txt and the copied source record. A present-but-
            # malformed value cannot be proven clean without knowing what
            # shape it actually is, so this fails CLOSED the same way an
            # unparseable Claims block already does, rather than treating
            # "malformed" as a synonym for "absent".
            return None
        redacted = 0
        new_value = []
        for item in value:
            if not isinstance(item, dict):
                # A list entry that is not a `{file: ..., role: ...}`
                # mapping at all - a bare scalar (`files: [/Users/alice/
                # Secret/private.pdf]`) is the shape round-11 audit finding
                # 2 names, but any other non-mapping shape (a nested list,
                # say) is just as unreadable here - is silently skipped by
                # every check below, which only ever inspects `item.get(...)`
                # after already confirming `isinstance(item, dict)`. That
                # skip is not "nothing to redact", it is "never even
                # looked": a raw path spelled directly as a list element
                # instead of wrapped in a `file:` mapping shipped completely
                # unexamined, in both README.txt and the copied record. Fail
                # CLOSED the same way an unparseable Claims entry already
                # does (`_redact_source_record_text`) rather than trusting a
                # shape this function cannot even check for a `file:` key.
                return None
            if isinstance(item.get('file'), str):
                new_file = _redact_asset_path(item['file'])
                if new_file != item['file']:
                    item = {**item, 'file': new_file}
                    redacted += 1
            new_value.append(item)
        if not redacted:
            return fm_text, 0
        eol = '\r\n' if key_line.endswith('\r\n') else ('\n' if key_line.endswith('\n') else '')
        dumped = yaml.safe_dump(
            new_value, default_flow_style=True, sort_keys=False, width=10 ** 6,
        ).strip()
        new_line = f'files: {dumped}{eol}'
        return fm_text[:key_start] + new_line + fm_text[key_end:], redacted

    # Block form - the shape every scaffolded record actually uses.
    block_end = key_end
    for line in lines[key_idx + 1:]:
        content = line.strip()
        if (line.startswith((' ', '\t')) or not content
                or content == '-' or content.startswith('- ') or content.startswith('#')):
            block_end += len(line)
            continue
        break
    block = fm_text[key_end:block_end]
    try:
        doc = yaml.safe_load(fm_text[key_start:block_end])
    except yaml.YAMLError:
        return None
    value = doc.get('files') if isinstance(doc, dict) else None
    if value is None:
        return fm_text, 0
    if not isinstance(value, list):
        # Same present-but-malformed shape as the inline arm above (round-
        # 10 audit, issue #170 finding 1) - a block-form `files:` whose
        # value parses to a mapping rather than a list (`files:` followed
        # by an indented `file: /Users/alice/private.pdf` with no leading
        # `- `, say) used to fall through this check exactly like an
        # absent key and ship unredacted. Fail closed instead of passing
        # it through: a value this cannot classify as a proper list is not
        # provably clean.
        return None
    spans = _yaml_list_item_spans(block)
    if spans is None or len(spans) != len(value):
        return None
    redacted = 0
    cursor = 0
    parts: list[str] = []
    for item, (s, e) in zip(value, spans):
        entry_text = block[s:e]
        if not isinstance(item, dict):
            # The block-form twin of the inline-form check above (round-11
            # audit, finding 2): a bullet entry that is not a `{file: ...}`
            # mapping at all - `files:\n  - /Users/alice/Secret/private.pdf`
            # (a bare scalar bullet, no `file:` key) - has no `.get('file')`
            # for the check below to even ask, so it always fell through
            # silently unexamined, raw path and all. Fail closed rather
            # than ship an entry this function never actually inspected.
            return None
        if isinstance(item.get('file'), str):
            raw_file = item['file']
            new_file = _redact_asset_path(raw_file)
            if new_file != raw_file:
                new_entry_text, n = _rewrite_file_key_in_entry(entry_text, new_file)
                if n != 1:
                    return None
                entry_text = new_entry_text
                redacted += 1
        parts.append(block[cursor:s])
        parts.append(entry_text)
        cursor = e
    if not redacted:
        return fm_text, 0
    parts.append(block[cursor:])
    new_block = ''.join(parts)
    return fm_text[:key_end] + new_block + fm_text[block_end:], redacted


def _redact_source_record_files_field(text: str) -> tuple[str, int] | None:
    """Redact any non-portable `files:` entry in a source record's own
    frontmatter before it is copied into a packet - the record-level
    companion to `_redact_asset_path`'s README-line redaction (issue #170
    finding 1, P1 follow-up; see `_redact_frontmatter_files_field`'s
    docstring for the leak this closes and why it is separate from the
    README's own redaction).

    Same shape as `_redact_source_record_text`: locate the frontmatter with
    `FRONT_RE`, hand the inner YAML text to the field-level redactor, and
    splice the result back in. Returns (text, 0) when there is no
    frontmatter at all (nothing structured to redact - matches
    `_redact_profile_text`'s same no-op reading for a record that somehow
    has none) or nothing needed redacting; None when something did and
    could not be safely edited, so the caller fails closed."""
    fm = FRONT_RE.match(text)
    if not fm:
        return text, 0
    fm_start, fm_end = fm.start(1), fm.end(1)
    fm_text = text[fm_start:fm_end]
    result = _redact_frontmatter_files_field(fm_text)
    if result is None:
        return None
    new_fm_text, redacted = result
    if not redacted:
        return text, 0
    return text[:fm_start] + new_fm_text + text[fm_end:], redacted


def _strip_frontmatter_list_entries(
    fm_text: str, key: str, should_strip,
) -> tuple[str, int, list] | None:
    """Remove the entries `should_strip` matches from a top-level frontmatter list.

    Handles the two shapes hand-written frontmatter uses: a block list under
    `key:` (each entry a `- …` bullet, possibly spanning lines) and an inline
    flow list (`key: [a, b]`) on the key line itself. Only the removed entries'
    own lines are touched; when every entry goes, the then-empty `key:` line
    goes too (block form) so the copy doesn't carry a dangling key.

    Returns (new_fm_text, removed_count, removed_items); (fm_text, 0, []) when
    the key is absent, holds no list, or nothing matches; None when a list is
    present but its entries cannot be safely matched to their lines - the
    caller must fail closed."""
    lines = fm_text.splitlines(keepends=True)
    key_re = re.compile(rf'^{re.escape(key)}\s*:\s*(.*)$')
    key_idx = None
    key_start = 0
    inline_rest = ''
    offset = 0
    for i, line in enumerate(lines):
        if not line.startswith((' ', '\t')):
            m = key_re.match(line.rstrip('\r\n'))
            if m:
                key_idx = i
                key_start = offset
                inline_rest = m.group(1).strip()
                break
        offset += len(line)
    if key_idx is None:
        return fm_text, 0, []
    key_line = lines[key_idx]
    key_end = key_start + len(key_line)

    if inline_rest and not inline_rest.startswith('#'):
        # Inline flow form: the whole list lives on the key line, so the
        # "surgery" is rewriting that one line (or dropping it entirely).
        try:
            doc = yaml.safe_load(key_line)
        except yaml.YAMLError:
            return None
        value = doc.get(key) if isinstance(doc, dict) else None
        if not isinstance(value, list):
            return fm_text, 0, []
        kept: list = []
        removed_items: list = []
        for item in value:
            (removed_items if should_strip(item) else kept).append(item)
        if not removed_items:
            return fm_text, 0, []
        eol = '\r\n' if key_line.endswith('\r\n') else ('\n' if key_line.endswith('\n') else '')
        if kept:
            dumped = yaml.safe_dump(
                kept, default_flow_style=True, sort_keys=False, width=10 ** 6,
            ).strip()
            new_line = f'{key}: {dumped}{eol}'
        else:
            new_line = ''
        return (
            fm_text[:key_start] + new_line + fm_text[key_end:],
            len(removed_items),
            removed_items,
        )

    # Block form: the list is the indented (or bulleted) lines that follow.
    block_end = key_end
    for line in lines[key_idx + 1:]:
        content = line.strip()
        if (line.startswith((' ', '\t')) or not content
                or content == '-' or content.startswith('- ') or content.startswith('#')):
            block_end += len(line)
            continue
        break
    block = fm_text[key_end:block_end]
    try:
        doc = yaml.safe_load(fm_text[key_start:block_end])
    except yaml.YAMLError:
        return None
    value = doc.get(key) if isinstance(doc, dict) else None
    if value is None or not isinstance(value, list):
        # A bare `key:` or a non-list value carries no list entries to strip
        # (consumers iterate these fields as lists; anything else reads as
        # nothing, so nothing can leak from it either).
        return fm_text, 0, []
    spans = _yaml_list_item_spans(block)
    if spans is None or len(spans) != len(value):
        return None
    remove: list[tuple[int, int]] = []
    removed_items = []
    for item, span in zip(value, spans):
        if should_strip(item):
            removed_items.append(item)
            remove.append(span)
    if not remove:
        return fm_text, 0, []
    for s, e in reversed(remove):
        block = block[:s] + block[e:]
    if len(remove) == len(value):
        return fm_text[:key_start] + block + fm_text[block_end:], len(removed_items), removed_items
    return fm_text[:key_end] + block + fm_text[block_end:], len(removed_items), removed_items


def _flatten_alias_strings(value) -> list[str]:
    """Depth-first strings inside a nested-list alias entry.

    An unquoted `[[Old Name]]` YAML-parses to nested lists, and the nesting
    depth differs between a block-form bullet (`- [[Old Name]]` gives a list
    in a list) and a flow-form list (`aliases: [[Old Name]]` gives one level
    less), so flatten all the way down rather than guess the depth."""
    if isinstance(value, list):
        out: list[str] = []
        for v in value:
            out.extend(_flatten_alias_strings(v))
        return out
    if value is None or isinstance(value, dict):
        return []
    return [str(value)]


def _redact_profile_text(
    text: str, *, include_restricted: bool, include_dna: bool,
) -> tuple[str, int] | None:
    """Strip withheld `name_variants` entries (and their `aliases:` mirrors)
    from a profile copy's frontmatter.

    A `{value:, restricted: …}` name variant is a private prior name (SPEC
    §19); TOOLING §8 applies the shared flag logic to a NAME like anything
    else, so a plain restriction opens with --include-restricted, dna with
    --include-dna, and by-request never ships. The alias mirror matters
    because owners copy variant values into `aliases:` for link resolution -
    stripping one carrier but not the other would still print the name. A
    mirror may be authored in any of the forgiving forms the alias consumers
    resolve (_lib.link_field_refs' catalogue): a plain string, a quoted
    wikilink (`"[[Old Name]]"`), or an unquoted `[[Old Name]]` that
    YAML-parses to a nested list - all three are matched through
    strip_link_wrapper (round-2 finding 5: matching only the plain form left
    the wrapped mirrors printing the very name the README said was removed).
    Body prose is untouched: the packet is a private export, and the
    structured entries are the only spec'd carriers of a withheld name.

    Returns (new_text, names_removed) - (text, 0) when there is nothing to
    strip - or None when a variants list exists but cannot be safely edited;
    the profile is the packet's required centerpiece, so the caller treats
    None as a structural build failure rather than shipping it unredacted.

    The no-frontmatter arm below reads as "nothing to strip", which is the
    right answer to the question this function asks (there are no structured
    name carriers) and the WRONG answer to the privacy question, since a
    profile with no frontmatter cannot state its `restricted:` marker either.
    That is why the subject gate in `_packet_payload` settles the marker
    through `_record_restriction` FIRST and refuses: by the time this runs, the
    profile is known to have a frontmatter block. Keep it that way round - a
    reader who moves the gate later re-opens the hole this arm cannot see."""
    fm = FRONT_RE.match(text)
    if not fm:
        return text, 0
    fm_start, fm_end = fm.start(1), fm.end(1)
    fm_text = text[fm_start:fm_end]

    def _strip_variant(item) -> bool:
        return isinstance(item, dict) and not _restricted_included(
            item.get('restricted'),
            include_restricted=include_restricted, include_dna=include_dna,
        )

    result = _strip_frontmatter_list_entries(fm_text, 'name_variants', _strip_variant)
    if result is None:
        return None
    fm_text, removed, removed_items = result
    if not removed:
        return text, 0

    hidden_values = {
        str(item.get('value') or '').strip().lower()
        for item in removed_items if isinstance(item, dict)
    }
    hidden_values.discard('')
    if hidden_values:
        def _strip_alias(item) -> bool:
            if isinstance(item, dict):
                return False
            if isinstance(item, list):
                parts = _flatten_alias_strings(item)
                target = strip_link_wrapper(f'[[{" ".join(parts)}]]') if parts else ''
            else:
                target = strip_link_wrapper(str(item))
            return target.strip().lower() in hidden_values

        alias_result = _strip_frontmatter_list_entries(fm_text, 'aliases', _strip_alias)
        if alias_result is None:
            return None
        fm_text, alias_removed, _ = alias_result
        removed += alias_removed

    return text[:fm_start] + fm_text + text[fm_end:], removed


def _strip_profile_drafts(text: str) -> tuple[str, int, str | None]:
    """Withhold unaccepted AI-draft prose from the profile copy (round-2 S1).

    The AI-pass contract (AGENTS.md) is unqualified: prose an AI drafted stays
    inside `<!-- AI-DRAFT ... -->` markers until `fha confirm draft` accepts
    it - no export ships it, and no packet flag opens it (the include flags
    govern the `restricted` marker, a different promise with a different
    gate). The packet is a private family export, so the posture mirrors
    `fha site`: WITHHOLD the draft blocks and keep building rather than
    refuse the packet - a draft is a normal in-progress state, not a defect -
    and say plainly in the README how much was held back. Accepted blocks
    ship with their AI-ACCEPTED provenance markers removed, like every other
    publication path.

    Only the body is stripped: the shared stripper cuts each draft back to
    the previous heading/marker boundary, so run over the whole file a draft
    at the top of the body would cut from offset 0 - straight through the
    frontmatter. Splitting first mirrors wikitree, which strips rec['body'].

    Returns (new_text, draft_blocks_withheld, problem). A non-None problem
    means a damaged marker (an unterminated `<!-- AI-DRAFT`, a stray marker
    word the grammar cannot account for): draft can no longer be told from
    accepted prose, no usable text is returned, and the caller must treat
    the profile as un-shippable - the same structural posture as a private
    name that could not be separated out. The block count comes from _lib's
    own marker regex, not a local copy, so the README's "N draft paragraphs
    were left out" can never drift from what the stripper actually cut."""
    fm = FRONT_RE.match(text)
    body_start = fm.end() if fm else 0
    body = text[body_start:]
    stripped, problem = strip_unaccepted_drafts(body)
    if problem is not None:
        return '', 0, problem
    return text[:body_start] + stripped, len(_AI_DRAFT_MARK_RE.findall(body)), None


# A purpose block (SPEC §16a, #75) is a blockquote whose every line starts
# `> ` - but a hand-authored `>` reply-quote is the SAME shape (a whole
# paragraph of consecutive `>`-prefixed lines), so this can only be told
# apart from one by POSITION, never by shape alone: the purpose block is
# always the very first thing in the body - straight after the frontmatter
# for a source record (no H1), or straight after the H1 for a person/
# research record (SPEC §16a/§21b). `_purpose_block_prefix_end` finds that
# position; the match below is anchored there via `pos`, never a bare
# `re.search`/`sub(count=1)` over the whole body, so a human's own
# blockquote written later - quoting an obituary in `## Biography`, say - is
# never mistaken for this one. Matches the block plus the one blank line
# that always follows it, so removing it never leaves a stray gap.
_PURPOSE_BLOCK_RE = re.compile(r'^(?:>[^\n]*\n)+\n?', re.M)


def _purpose_block_prefix_end(body: str) -> int:
    """Return the index in `body` (already past the frontmatter) where a
    purpose block would begin if this record has one.

    Skips any leading blank lines (a source record's body opens directly
    with a blank line then the block; a person/research record's opens with
    a blank line, the H1, then another blank line before the block - and a
    record that happens to be missing one of those blank lines must still
    resolve to the same spot) then, if what follows is an H1 line (`# `,
    never `## `), skips past it and any blank lines after it too. Whatever
    position remains is where `_PURPOSE_BLOCK_RE` is anchored - not searched
    for - next."""
    i, n = 0, len(body)
    while i < n and body[i] in '\r\n':
        i += 1
    if body[i:i + 2] == '# ':
        nl = body.find('\n', i)
        i = n if nl == -1 else nl + 1
        while i < n and body[i] in '\r\n':
            i += 1
    return i


# The ## Sources GENERATED-BEGIN/END region (#76), matched by its OWN heading
# to the next `## ` heading or EOF - the same span `_lib.section_bounds`
# would return, reimplemented as one regex here rather than importing that
# helper for a single narrow use.
_SOURCES_REGION_RE = re.compile(r'^## Sources\r?\n.*?(?=^## |\Z)', re.M | re.S)


def _strip_scaffolding_blocks(text: str, *, is_person_profile: bool) -> str:
    """Remove #75's visible purpose block - and, for a person profile, #76's
    `## Sources` region plus any §16 section still holding only the record
    template's own authoring instructions - from a copy about to leave the
    archive.

    Scaffolding for the WORKING archive, not content for the family (the
    same principle `_strip_profile_drafts`/`strip_unaccepted_drafts` already
    apply to unaccepted AI-DRAFT prose): a packet recipient does not need to
    be told which parts of the file are machine-owned, because the file
    itself is now a static copy nobody is meant to edit.

    The `## Sources` region is dropped ENTIRELY, not just its markers: it
    lists every source touching the person by ARCHIVE-RELATIVE path
    (meaningless once copied out of the archive), and is not privacy-filtered
    the way the packet's own file-gathering already is - a source excluded
    from this packet for a restricted/DNA/living reason would otherwise still
    be NAMED in a raw copy of that list. No substitute is generated either -
    the packet does not rebuild a privacy-safe sources listing, the same
    choice `_build_timeline_text` already makes for the timeline (a fresh,
    filtered build, never a copy of the archive's own view file); the shipped
    source RECORDS and FILES (already privacy-filtered elsewhere in the
    build) are what a recipient reads instead.

    An UNFILLED §16 section (#125) is the same kind of thing one level down:
    `## Biography` holding nothing but "Write their story in plain
    sentences..." is the template talking to the archive OWNER, and a
    relative opening this packet reads it as what the family had to say
    about the person. `fha site` already omits those sections from a page;
    `_lib.strip_unfilled_person_sections` is the shared check, so the packet
    and the site can never disagree about which sections were actually
    written. An empty section goes too - a `## Stories` heading over nothing
    promises content the record does not have.

    Only person profiles carry `## Sources` and the §16 sections
    (`is_person_profile=True`); source records and research companions carry
    neither, so packet's other two callers pass False and get the purpose
    block stripped and nothing else.

    Bounded to the BODY (never the frontmatter) the same way `_strip_profile_
    drafts` bounds itself. The purpose-block match is POSITION-anchored, not
    a bare first-match `sub(count=1)` over the whole body: it only ever
    strips a blockquote run sitting immediately after the H1 (person/
    research) or immediately after the frontmatter (source, which has no
    H1) - never one found merely by searching forward, which would also
    match - and silently eat - a human's own blockquote written later in
    their Biography (a quoted obituary, letter, or inscription is common
    genealogical prose, and looks identical in shape to this block).
    """
    fm = FRONT_RE.match(text)
    body_start = fm.end() if fm else 0
    body = text[body_start:]
    if is_person_profile:
        body = _SOURCES_REGION_RE.sub('', body)
        body = strip_unfilled_person_sections(body)
    prefix_end = _purpose_block_prefix_end(body)
    block = _PURPOSE_BLOCK_RE.match(body, prefix_end)
    if block:
        body = body[:prefix_end] + body[block.end():]
    return text[:body_start] + body


_REQUIRED_TABLES = (
    'persons', 'claims', 'sources', 'claim_persons', 'source_files',
    'source_people', 'person_files', 'citations',
)

_IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.heic', '.bmp', '.gif'}


def _today() -> str:
    return datetime.date.today().isoformat()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _curated_person(conn: sqlite3.Connection, pid: str) -> sqlite3.Row | None:
    """Return the persons row for pid, or None if absent. Caller checks tier."""
    return conn.execute(
        'SELECT id, name, surname, living, tier, status, merged_into, path FROM persons WHERE id = ?',
        (pid,),
    ).fetchone()


def _resolve_merged_person(
    conn: sqlite3.Connection, person: sqlite3.Row
) -> tuple[sqlite3.Row, list[str]]:
    """
    Follow `merged_into` to the survivor (SPEC §9: "tools resolve
    references through merged_into"). A merged tombstone's own `tier`/
    `living` are irrelevant once redirected - the survivor's gate checks
    apply instead. Guards against a corrupt merge cycle by capping the
    chain length rather than looping forever.
    """
    notes: list[str] = []
    seen = {person['id']}
    while person['status'] == 'merged' and person['merged_into']:
        target_id = person['merged_into']
        if target_id in seen:
            notes.append(f'{fmt_id_display(target_id)}: merge chain cycle detected; stopping redirect.')
            return person, notes
        target = _curated_person(conn, target_id)
        if target is None:
            notes.append(
                f'{fmt_id_display(person["id"])} is merged into {fmt_id_display(target_id)}, '
                'which is not in the index.'
            )
            return person, notes
        notes.append(
            f'{fmt_id_display(person["id"])} is merged into {fmt_id_display(target_id)}; '
            'building the packet for the survivor.'
        )
        seen.add(target_id)
        person = target
    return person, notes


def _merged_alias_ids(conn: sqlite3.Connection, survivor_id: str) -> list[str]:
    """
    Every person id whose merged_into chain resolves to survivor_id (SPEC
    §9), found by walking merged_into outward from the survivor rather
    than assuming a single hop. Once `_resolve_merged_person` redirects pid
    to the survivor, sources/claims still citing one of these old ids must
    still be gathered, not dropped.
    """
    aliases: set[str] = set()
    frontier = {survivor_id}
    while frontier:
        placeholders = ','.join('?' * len(frontier))
        rows = conn.execute(
            f"SELECT id FROM persons WHERE status = 'merged' AND merged_into IN ({placeholders})",
            list(frontier),
        ).fetchall()
        frontier = {r['id'] for r in rows if r['id'] not in aliases and r['id'] != survivor_id}
        aliases |= frontier
    return sorted(aliases)


def _source_ids_for_person(conn: sqlite3.Connection, pids: list[str]) -> list[str]:
    """
    Distinct source IDs citing any of pids - the same two-table UNION
    views.py uses for sources-index (claim_persons→claims, plus the direct
    source_people table for sources that name someone without yet having
    extracted claims). Duplicated here rather than imported: tools never
    import tools (TOOLING §15).

    pids carries the survivor plus any merged-away aliases (SPEC §9) so a
    source that still cites an old id isn't dropped from the packet.
    """
    placeholders = ','.join('?' * len(pids))
    rows = conn.execute(
        f"""
        SELECT DISTINCT c.source_id
        FROM claim_persons cp
        JOIN claims c ON cp.claim_id = c.id
        WHERE cp.person_id IN ({placeholders})
        UNION
        SELECT DISTINCT source_id
        FROM source_people
        WHERE person_id IN ({placeholders})
        """,
        list(pids) + list(pids),
    ).fetchall()
    return [r[0] for r in rows]


def _source_restricted_value(archive_root: Path, row: sqlite3.Row) -> tuple[object, str | None]:
    """The source's `restricted:` value, for the export decision. Returns
    (value, trouble) - `trouble` non-None means the record's own marker could
    not be read and the caller must exclude the source outright.

    The index stores `restricted` only as 0/1, so a free-text type
    (`restricted: by-request` on a source) is lost there - the type is read from
    the `.md` frontmatter. The two are combined rather than one overriding the
    other: if the file states a value it wins (it carries the type), otherwise
    the index's 1 still counts as a plain restriction, and a DNA source_type is
    always treated as restricted (lint E017) even if the flag was hand-dropped.

    IT USED TO FALL BACK TO THE INDEX ON AN UNREADABLE RECORD, and its docstring
    called that "fail closed". It was the opposite. The index's 0 (the common
    case - the column is only 1 when some earlier index run read a marker) made
    an unreadable source read as unrestricted and ship, files and title and all.
    Worse, its 1 cannot carry a type: an unreadable `restricted: by-request`
    source degraded to a plain restriction, which `--include-restricted` opens -
    turning the one no-override type in AGENTS.md's contract item 6 into an
    overridable one. Combining a value with a value is right; combining a value
    with a GUESS is not, and the index bit is a guess about a file nobody could
    read. `by-request` is precisely what cannot be ruled out, so an unreadable
    record now opens under no flag at all. The cost is bounded and recoverable:
    the source is still named in the README (ID + reason, no title), and the
    warning names the record and the command that repairs it."""
    value, trouble = _record_restriction(archive_root / row['path'])
    if trouble is not None:
        return None, trouble
    if value in (None, False, '', 'false'):
        if (row['source_type'] or '') == 'dna':
            return 'dna', None
        if (row['restricted'] or 0):
            return 'true', None
        return None, None
    return value, None


def _classify_sources(
    conn: sqlite3.Connection,
    archive_root: Path,
    source_ids: list[str],
    *,
    include_restricted: bool,
    include_dna: bool,
    messages: list[str],
) -> tuple[list[sqlite3.Row], list[sqlite3.Row], set[str]]:
    """
    Split source_ids into (included, excluded, unreadable) per TOOLING §8's
    privacy rules; `unreadable` is the subset of `excluded` whose own record
    could not be read, which the README reports as its own reason rather than
    miscalling it "restricted".

    The `restricted` marker is read from each source record (so a free-text type
    like `restricted: by-request` is honored, not just the index's 0/1), and the
    shared decision applies the no-override rule: `dna` needs --include-dna,
    `by-request` is never opened, everything else (incl. the plain boolean) needs
    --include-restricted. A record whose marker could not be read is excluded
    under every flag - see `_source_restricted_value`.

    Excluding at THIS step rather than later is deliberate: `included_ids` is
    the single filter the timeline, the copy loop and the asset gather all read,
    so a source dropped here is dropped from every surface at once. A withhold
    bolted on further down would have to be repeated at each of them, and the
    one that got missed would be the leak.
    """
    if not source_ids:
        return [], [], set()
    placeholders = ','.join('?' * len(source_ids))
    rows = conn.execute(
        f"""
        SELECT id, title, source_type, restricted, path
        FROM sources WHERE id IN ({placeholders})
        ORDER BY title
        """,
        source_ids,
    ).fetchall()

    included, excluded = [], []
    unreadable: set[str] = set()
    for row in rows:
        value, trouble = _source_restricted_value(archive_root, row)
        if trouble is not None:
            excluded.append(row)
            unreadable.add(row['id'])
            messages.append(
                f'WARNING: {fmt_id_display(row["id"])} ({row["path"]}) could not be '
                f'read ({trouble}), so there is no way to tell whether that source - '
                'or any fact in it - was marked private. It was left out of the '
                'packet, along with its files. Repair that record (run `fha lint` '
                'to see what is wrong), run `fha index`, then build the packet again.'
            )
            continue
        if _restricted_included(value, include_restricted=include_restricted, include_dna=include_dna):
            included.append(row)
        else:
            excluded.append(row)
    return included, excluded, unreadable


def _other_named_persons(
    conn: sqlite3.Connection, included_source_ids: list[str], pid: str
) -> list[sqlite3.Row]:
    """
    Return living/unknown persons (other than pid) named by an included
    source's claims or its source_people list - the README caution set
    (TOOLING §8: "any *other* person ... with living: true is named in a
    README caution"). living: unknown counts as living throughout.
    """
    if not included_source_ids:
        return []
    placeholders = ','.join('?' * len(included_source_ids))
    rows = conn.execute(
        f"""
        SELECT DISTINCT p.id, p.name
        FROM persons p
        WHERE p.id != ? AND p.living IN ('true', 'unknown') AND p.id IN (
            SELECT cp.person_id FROM claim_persons cp
            JOIN claims c ON cp.claim_id = c.id
            WHERE c.source_id IN ({placeholders})
            UNION
            SELECT person_id FROM source_people WHERE source_id IN ({placeholders})
        )
        ORDER BY p.name
        """,
        [pid] + included_source_ids + included_source_ids,
    ).fetchall()
    return rows


def _citation_named_persons(
    conn: sqlite3.Connection, copied_paths: set[str], pid: str
) -> list[sqlite3.Row]:
    """
    Return living/unknown persons (other than pid) named by a bare `[P-id]`
    citation token anywhere in the packet's copied .md files (profile,
    research note, included source records) - catches a living person
    mentioned only in prose, with no `claim_persons`/`source_people` row,
    that `_other_named_persons` would otherwise miss.
    """
    if not copied_paths:
        return []
    placeholders = ','.join('?' * len(copied_paths))
    rows = conn.execute(
        f"""
        SELECT DISTINCT p.id, p.name
        FROM persons p
        WHERE p.id != ? AND p.living IN ('true', 'unknown') AND p.id IN (
            SELECT token FROM citations WHERE kind = 'P' AND path IN ({placeholders})
        )
        ORDER BY p.name
        """,
        [pid] + list(copied_paths),
    ).fetchall()
    return rows


def _is_under_root(path: Path, root: Path) -> bool:
    """True if `path` is inside `root` (both resolved); False on unrelated trees.

    A deliberate local twin of process.py's `_is_under` (tools never import
    tools - TOOLING §15).
    """
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


# Unicode characters that visually resemble a real path separator, mapped to
# the ASCII separator they impersonate - used only to detect and split a
# foreign-looking path in `_redact_asset_path`/`_is_bare_directory_reference`
# below, never to alter what gets shown for an ordinary alias (adversarial
# review, round 8 audit; see `_redact_asset_path`'s docstring for the leak
# this closes).
_PATH_SEPARATOR_LOOKALIKES = {
    '⁄': '/',    # FRACTION SLASH
    '∕': '/',    # DIVISION SLASH
    '／': '/',    # FULLWIDTH SOLIDUS
    '⧸': '/',    # BIG SOLIDUS
    '＼': '\\',   # FULLWIDTH REVERSE SOLIDUS
    '⧵': '\\',   # REVERSE SOLIDUS OPERATOR
}


def _normalize_path_separator_lookalikes(raw: str) -> str:
    """Replace any `_PATH_SEPARATOR_LOOKALIKES` character with the real
    separator it impersonates. `Path.is_absolute()`, `PureWindowsPath`'s
    parsing, and the leading-character checks in `_redact_asset_path` all
    recognize ONLY ASCII `/`/`\\` - a path using a homoglyph instead splits
    into far fewer components than it actually has, which is exactly the
    "not really absolute"/"not really multi-part" confusion this whole
    function exists to close for the real separators (see
    `_redact_asset_path`'s own docstring on `Path.is_absolute()` and
    `PureWindowsPath`). Applied only where a path is being SPLIT or
    CLASSIFIED, never to what is actually shown for an ordinary, already-
    portable alias."""
    for lookalike, real in _PATH_SEPARATOR_LOOKALIKES.items():
        raw = raw.replace(lookalike, real)
    return raw


def _strip_invisible_format_chars(raw: str) -> str:
    """Strip any LEADING or TRAILING Unicode FORMAT character (general
    category `Cf`: a zero-width space, a byte-order mark, a right-to-left
    override, a word joiner, and similar characters no text editor renders
    visibly) from `raw`.

    `_redact_asset_path`'s leading-character checks read `normalized[0]`/
    `normalized[1]` positionally, and `str.strip()` only removes
    WHITESPACE (`str.isspace()`) - every `Cf` character fails that test, so
    one sitting at position 0 sails through `.strip()` untouched and
    defeats those checks exactly like a visible leading space does
    (`_redact_asset_path`'s own round-9 finding), just invisibly (this
    function's own adversarial self-review, round-10 audit, after fixing
    four Codex-found bypasses on the same surface). Category, not a fixed
    character list, so this covers the zero-width space AND join AND
    directional-override family uniformly rather than naming each one by
    hand. Applied only where a path is being CLASSIFIED, same scope as
    `_normalize_path_separator_lookalikes` above - never to what is
    actually shown for an ordinary, already-portable alias."""
    start = 0
    while start < len(raw) and unicodedata.category(raw[start]) == 'Cf':
        start += 1
    end = len(raw)
    while end > start and unicodedata.category(raw[end - 1]) == 'Cf':
        end -= 1
    return raw[start:end]


def _redact_asset_path(raw: str) -> str:
    """Render a `files:` entry for a packet's README: unchanged if it is a
    normal relative alias ('documents/...', 'photos/...'), basename-only if
    it looks like an absolute filesystem path or a home-directory shorthand.

    A stored `files:` entry is meant to be a portable alias (TOOLING: "all
    stored paths are alias-form"), but nothing stops a hand-edit from
    replacing one with a real absolute path off the archive owner's own
    machine - and a packet is exported to a family member outside that
    machine (issue #170 finding 1, round-3 audit). `_resolve_source_files`
    already keeps such an entry's ASSET out of the packet (the containment
    check below), but before this it still copied the raw string straight
    into `missing_assets`, and from there into README.txt - disclosing the
    owner's username, drive layout, or private directory names even though
    the file itself was correctly omitted. An ordinary alias is left exactly
    as-is: that is useful diagnostic information a human can act on, not a
    leak.

    Checked by leading CHARACTER, not `Path(raw).is_absolute()` alone
    (adversarial review, round-4 audit): a portable alias never starts with
    a path separator, `~`, or a drive letter, so any of those on the raw
    string is already disqualifying regardless of what the current OS's
    `pathlib` considers absolute. `Path.is_absolute()` is platform-relative
    and was the actual bug - `WindowsPath('/Users/name/secret.pdf').is_absolute()`
    is `False` (Windows "absolute" requires a drive or UNC root), so a
    POSIX-style absolute path leaked through whole on an archive owner's
    Windows machine; `~andrew_sielen/Documents/secret.pdf` (shell home-
    directory shorthand, which nothing here ever expands) leaked a literal
    username through the exact same gap, on any OS, since `pathlib` never
    treats `~` as meaningful on its own. `Path(raw).is_absolute()` is kept
    as a catch-all fourth check for any OS-specific absolute form the three
    character checks don't anticipate, not as the primary test anymore.

    The BASENAME is extracted with `PureWindowsPath`, unconditionally, not
    `Path` (Codex review, round-5 audit - the mirror-image of the bug just
    above): this archive's packet can be built on any OS, and a `files:`
    entry can name a foreign path from a DIFFERENT OS than the one doing
    the building. `Path(raw)` resolves to whichever OS-specific class the
    CURRENT host uses - on POSIX, `PosixPath('C:\\Users\\andrew\\secret.pdf').name`
    never recognizes `\\` as a separator at all and returns the entire raw
    string unchanged, shipping the username straight into README.txt on a
    Linux/Mac-built packet (this is exactly what CI's Linux runners caught
    - the prior fix's own new Windows-path tests only ever ran, and passed,
    on the Windows machine that authored them). `PureWindowsPath` never
    touches the filesystem and understands BOTH separator styles (`/` and
    `\\`) plus drive letters and UNC roots on every host OS alike, so a
    Windows-shaped foreign path redacts correctly when the packet is built
    on POSIX, and a POSIX-shaped foreign path (forward-slash-separated)
    still redacts correctly too - `PureWindowsPath` is a strict superset of
    `PurePosixPath`'s separator handling for this purpose (real per-OS
    filesystem calls are never made here, only string splitting to find the
    trailing component).

    A hand-edited entry can also be a genuine absolute path - no `~`
    involved at all - that happens to END at a directory named after the
    owner's username: `/Users/andrew_sielen/` or `C:\\Users\\andrew_sielen\\`
    (Codex review, round-7 audit). Neither starts with `~`, so the round-6
    bare-shorthand check above never sees them, and `PureWindowsPath(...).name`
    still returns `'andrew_sielen'` - the last PATH COMPONENT, which here is
    a directory (in fact the username itself), not a file. The general
    invariant is checked directly, before basename extraction, by
    `_is_bare_directory_reference`: see its docstring for the two
    unambiguous shapes it recognizes.

    A hand-edit made through a YAML editor can also leave surrounding
    whitespace on the value - a quoted scalar like `" /Users/andrew/Secret/
    scan.pdf"` parses to a string whose first character is a space, not `/`
    (post-merge Codex review of #176, round-9 audit). Every check above
    reads `normalized[0]`/`normalized[1]` positionally, so that leading
    space defeats all of them at once and the untouched raw value - leaking
    username and directory layout exactly like the bugs above - passed
    through as if it were an ordinary alias. Classification and basename
    extraction below both run against a STRIPPED copy for this reason; the
    untouched `raw` (whitespace included) is still what a genuinely
    portable, non-foreign alias gets back, so an ordinary entry's spelling
    is never silently rewritten.

    A `file://` URI (`file:///Users/alice/Secret/private.pdf`) is also
    recognized as foreign (issue #170 finding 1, round-10 audit - the
    fourth bypass a single post-merge review pass found after six already-
    patched rounds on this exact function): see the `file_uri_match` check
    inline below for why the leading-character checks above all miss it.

    A ZERO-WIDTH or otherwise invisible Unicode FORMAT character (a
    zero-width space, a byte-order mark, a right-to-left override - Unicode
    general category `Cf`) sitting at position 0 is the fifth bypass, not a
    sixth Codex round this time but this function's own adversarial
    self-review after the fourth: `str.strip()` above only removes
    WHITESPACE (`str.isspace()`), and every `Cf` character is `False` for
    that test - not the visible round-9 space, but the exact same defeat of
    every leading-CHARACTER check by the exact same mechanism (whatever
    sits at `normalized[0]` is not `/`/`~`/a real drive letter), just
    invisible in almost any editor instead of merely easy to miss. This is
    not a contrived shape either: a UTF-8-with-BOM save or a copy-paste out
    of a browser address bar leaves exactly this behind. `_strip_invisible_
    format_chars` runs immediately after `.strip()`, on the same
    classification-only copy.
    """
    if not raw:
        return raw
    stripped = _strip_invisible_format_chars(raw.strip())
    if not stripped:
        return raw
    # A Unicode character that visually resembles `/` or `\` but is neither
    # (adversarial review, round 8 audit) defeats every check below at once:
    # `Path.is_absolute()`, the leading-character checks just below, and
    # `PureWindowsPath`'s own separator handling all recognize ONLY the two
    # real ASCII separators, so a homoglyph-separated path collapses to far
    # fewer "parts" than it actually has - `/Users⁄andrew_sielen⁄
    # secret.pdf` looks foreign correctly (it still starts with a real `/`),
    # but its basename then extracts as the entire remainder,
    # `Users⁄andrew_sielen⁄secret.pdf` - the owner's username and
    # full directory structure, not a redacted basename. Normalizing first
    # closes this uniformly for `_is_bare_directory_reference` and the
    # basename extraction below alike; the ORIGINAL `raw` is still what gets
    # returned on the ordinary, non-foreign path, so a legitimate alias that
    # happens to contain one of these characters as ordinary text (not as a
    # separator) is never rewritten.
    normalized = _normalize_path_separator_lookalikes(stripped)
    # A `file://` URI (`file:///Users/alice/Secret/private.pdf`, or the
    # `file://host/share/...` form some tools emit for a network path) is
    # how a value copied out of a browser address bar or a "Copy as URI"
    # menu item spells an absolute local path - and it defeats every check
    # above at once (issue #170 finding 1, round-10 audit): the string
    # starts with `f`, not a separator/`~`/drive-letter, and
    # `Path(raw).is_absolute()` does not consider a URI STRING absolute
    # either - it is not a real filesystem path spelling, it is a scheme
    # plus one. `file_uri_match` is checked case-insensitively (URI schemes
    # are not case-sensitive) and accepts either the two-slash
    # (`file://host/path`) or three-slash (`file:///path`, empty
    # authority) form. When it matches, everything from the scheme on is
    # foreign by definition, and the classification/basename-extraction
    # steps below run against the URI's PATH PART (the scheme stripped
    # off) rather than the full string - otherwise `file:` itself would be
    # misread as a bogus leading path segment and could leak into the
    # "basename".
    #
    # The scheme marker consumed here is EXACTLY two slashes - `file://` -
    # never `file://+` (round-11 audit, post-merge Codex review of #197,
    # finding 3): the three-slash form's third slash is not part of the
    # scheme separator at all, it is the LEADING slash of the absolute
    # path that follows an empty authority (`file://` + `` + `/Users/alice`
    # per the URI grammar). A greedy `+` swallowed that slash along with
    # the scheme marker, so `file:///Users/alice` handed
    # `_is_bare_directory_reference`/the basename extraction below the
    # PATH PART `Users/alice` - two components, indistinguishable from an
    # ordinary relative alias - instead of the rooted `/Users/alice` the
    # URI actually names. `_is_bare_directory_reference`'s directory-root
    # check (`PureWindowsPath(raw).parts` having exactly three components:
    # the anchor, `Users`, and the name) depends entirely on that leading
    # slash surviving; without it, `file:///Users/alice` (no trailing
    # slash - a directory reference, nothing safe to show at all) fell
    # through to an ordinary basename extraction and returned the bare
    # username `alice` instead of `(unnamed path)`. Consuming only the
    # fixed two-slash marker leaves any further leading slash exactly
    # where the URI grammar puts it, for both forms alike - a real
    # `file://host/path` two-slash value is unaffected either way, since
    # it never has a third consecutive slash for `+` to have over-consumed
    # in the first place.
    file_uri_match = re.match(r'(?i)^file://', normalized)
    looks_foreign = (
        normalized[0] in ('/', '\\')
        or normalized[0] == '~'
        or (len(normalized) > 1 and normalized[1] == ':')
        or bool(file_uri_match)
        or Path(normalized).is_absolute()
    )
    if not looks_foreign:
        return raw
    path_part = normalized[file_uri_match.end():] if file_uri_match else normalized
    if _is_bare_directory_reference(path_part):
        return '(unnamed path)'
    name = PureWindowsPath(path_part).name or '(unnamed path)'
    # A bare `~`/`~user` shorthand with NO separator anywhere in it (no
    # trailing slash either - that shape is caught by
    # `_is_bare_directory_reference` above) has nothing real for
    # PureWindowsPath to extract, so `.name` returns the shorthand ITSELF
    # unchanged (Codex review, round-6 audit): `~andrew_sielen` -> the same
    # string back. That still ships the username straight into README.txt.
    # Checking the EXTRACTED name for a leading `~` (rather than the raw
    # string, which an earlier version of this fix did and which missed the
    # trailing-slash shape - now handled separately above) catches this
    # remaining variant: whatever PureWindowsPath handed back is still just
    # the shorthand, not a real trailing component, whenever it still starts
    # with `~`.
    if name.startswith('~'):
        return '(unnamed path)'
    return name


def _is_bare_directory_reference(raw: str) -> bool:
    """True when `raw` names a directory, not a real file - so there is
    nothing safe to show as a basename at all (Codex review, round-7 audit,
    issue #170 finding 1).

    Two shapes are unambiguous without ever touching the filesystem
    (`_redact_asset_path` only ever does string splitting, ``real per-OS
    filesystem calls are never made here`` per its own docstring):

    - A trailing separator. Whatever follows the LAST separator in a path
      that ENDS with one is empty by definition - there is no trailing file
      component at all, only a directory. `/Users/andrew_sielen/` and
      `C:\\Users\\andrew_sielen\\` are exactly this shape. This also
      subsumes the bare-tilde-with-trailing-slash case the round-6 fix
      handled as a special case of its own (`~andrew_sielen/`), so that
      variant no longer needs separate handling.
    - An absolute path that ends EXACTLY at a directory OS convention
      guarantees is never a file - `/Users/<name>`, `/home/<name>`,
      `C:\\Users\\<name>` (a user's home directory), or `/Volumes/<name>`
      (a macOS mount point - every mounted volume, local or network,
      appears here, and a mount point cannot itself be a file) - with
      nothing after it and no trailing separator either. Dropping the
      trailing slash from the shapes above reaches the identical leak
      (`PureWindowsPath('/Users/andrew_sielen').name` is still
      `'andrew_sielen'`) without tripping the check above, so it is
      recognized on its own terms: `PureWindowsPath(raw).parts` has exactly
      three components (the anchor, the convention name, and whatever comes
      after it) only when the path stops immediately there. A real file
      living somewhere under one of these (`/Users/andrew_sielen/secret.pdf`,
      or anything deeper) has a fourth part and is unaffected - only the
      bare root itself redacts.

    A bare `~user` with no separator anywhere (`~andrew_sielen`, one part,
    neither shape above) is NOT covered here - `_redact_asset_path` still
    catches it separately, since the leading `~` is itself the tell.

    Deliberately NOT attempted (adversarial review, round 8 audit): a
    personal-storage path with no OS-guaranteed directory marker at all -
    `/srv/backups/andrew_sielen`, `D:\\Backups\\andrew_sielen`,
    `/mnt/nas/family-shares/andrew_sielen` - still shows its trailing
    segment as a "basename" even when that segment happens to look like a
    username. `Backups`/`srv`/an arbitrary NAS share name carries no OS
    guarantee the way `Users`/`Volumes` does - the identical shape
    (`D:\\Documents\\report.pdf`) is an ordinary, everyday FILE reference,
    so treating every 3-part absolute path as a directory would silently
    swallow real, useful diagnostic filenames right alongside the rare
    genuine leak, with no way to tell the two apart without touching the
    filesystem (something this function's own docstring rules out). A
    fixed word-list of "backup-sounding" folder names would only ever cover
    the specific examples it was written against, at the cost of exactly
    that false-positive risk for everyone else - worse than the narrow gap
    it would close. This one stays a known, accepted limitation rather than
    a guess.
    """
    if raw.rstrip().endswith(('/', '\\')):
        return True
    parts = PureWindowsPath(raw).parts
    return len(parts) == 3 and parts[1].lower() in ('users', 'home', 'volumes')


def _resolve_source_files(
    conn: sqlite3.Connection,
    archive_root: Path,
    fha_config: dict,
    source_ids: list[str],
) -> tuple[dict[str, list[Path]], list[str]]:
    """Map source_id -> existing asset Paths, plus missing/unresolvable notes.

    Packet output should be useful even when a fixture or archive points at a
    missing file, but omission must not be silent: the caller writes these
    notes into README.txt and returns a warning exit.

    `source_files.path` is a straight copy of the source record's own
    hand-editable `files:` entry (index.py's INSERT), the same trust boundary
    `fha source clear-keyword` and `fha process refile` guard against a
    naive-string escape - a '..'-carrying entry like
    'documents/../../outside.tif' resolves outside the configured root, and
    without a containment check `_copy_into` would `shutil.copy2` whatever
    that resolves to straight into an exported packet a human hands to a
    family member. Every resolved path is required to land inside the
    configured documents or photos root before it is offered for copying;
    anything else is reported the same way a missing file already is, not
    silently included.

    Every note below names the offending entry via `_redact_asset_path`, not
    the raw stored string - an entry that is itself an absolute local path
    (rather than a portable alias) is shown as a basename only, so the
    README.txt these notes flow into never discloses the owner's username or
    drive layout to whoever the packet is handed to (issue #170 finding 1).
    """
    if not source_ids:
        return {}, []
    placeholders = ','.join('?' * len(source_ids))
    rows = conn.execute(
        f'SELECT source_id, path FROM source_files WHERE source_id IN ({placeholders})',
        source_ids,
    ).fetchall()
    documents_root = resolve_path('documents', fha_config, archive_root)
    photos_root = resolve_path('photos', fha_config, archive_root)
    out: dict[str, list[Path]] = {}
    missing: list[str] = []
    for row in rows:
        # Never echo the raw stored `files:` string into README.txt below -
        # if it is a hand-edited absolute path rather than a portable alias,
        # the raw string can carry the owner's username, drive layout, or
        # private directory names (issue #170 finding 1, round-3 audit). An
        # ordinary relative alias passes through unchanged; see
        # `_redact_asset_path`.
        shown_path = _redact_asset_path(str(row['path']))
        try:
            resolved = resolve_path(row['path'], fha_config, archive_root)
        except Exception as e:
            missing.append(
                f'{fmt_id_display(row["source_id"])} asset {shown_path!r} could not be resolved: {e}'
            )
            continue
        try:
            contained = (_is_under_root(resolved, documents_root)
                         or _is_under_root(resolved, photos_root))
        except RuntimeError:
            # `_is_under_root` calls `.resolve()` on both `resolved` and the
            # configured root; if either traverses a symlink loop, `.resolve()`
            # raises RuntimeError rather than returning - and unlike
            # ValueError/OSError, `_is_under_root` does not catch it (audit
            # finding, round 2). This function runs BEFORE the packet build's
            # own guarded write block and the CLI has no exception translation
            # here, so an uncaught RuntimeError would turn one malformed
            # filesystem entry into a bare traceback and NO packet at all,
            # instead of the graceful "omitted with a warning" every other
            # resolution failure above already gets. Catch it here and give the
            # same treatment: excluded, with a message naming what it almost
            # certainly is (a corrupted or maliciously crafted symlink loop,
            # not an ordinary missing/escaping path). The raw RuntimeError
            # text is deliberately NOT included (issue #170 finding 1, round-3
            # audit): `Path.resolve()` embeds the absolute, OS-resolved
            # filename that looped in its message, which can carry the same
            # username/drive-layout information `shown_path` above is already
            # redacting - a fixed, generic phrase says exactly as much as a
            # human handed this packet needs to know.
            missing.append(
                f'{fmt_id_display(row["source_id"])} asset {shown_path!r} could not be '
                'checked against the configured roots - this looks like a symlink '
                'loop, most likely from a corrupted or maliciously crafted filesystem '
                'entry, and was left out of the packet. Find and fix or remove the '
                'offending symlink, then build the packet again.'
            )
            continue
        if not contained:
            missing.append(
                f'{fmt_id_display(row["source_id"])} asset {shown_path!r} resolves outside '
                'the configured documents/photos roots - this looks like a hand-edited '
                'files: entry gone wrong (a \'..\' segment, or a doubled slash), and was '
                'left out of the packet. Run `fha lint` and fix the entry by hand.'
            )
            continue
        if resolved.exists():
            out.setdefault(row['source_id'], []).append(resolved)
        else:
            missing.append(
                f'{fmt_id_display(row["source_id"])} asset missing on disk: {shown_path}'
            )
    return out, missing


def _is_image_path(p: Path) -> bool:
    return p.suffix.lower() in _IMAGE_SUFFIXES


# ── Photo gathering ───────────────────────────────────────────────────────────

# `fha photoindex reconcile` keeps a vanished photo's row in the catalog under
# the synthetic key 'MISSING:' + its last known path, so the caption, keywords
# and date history it carried survive the file itself. The two helpers below
# are photoindex.py's own vocabulary, restated here because tools never import
# tools (TOOLING §15): _live_alias answers "where was this photo" and
# _is_missing_key answers "can this file be opened". A packet is a physical
# bundle of files, so every copy path must ask the second question first.
_MISSING_PREFIX = 'MISSING:'


def _live_alias(path: str) -> str:
    """The alias a cached photo key refers to, with any 'MISSING:' prefix off.

    The prefix decorates the path a photo had; it is not a different path. Use
    this whenever the answer is a place ("where did this photo live"), never to
    build something to open.
    """
    return path[len(_MISSING_PREFIX):] if path.startswith(_MISSING_PREFIX) else path


def _is_missing_key(path: str) -> bool:
    """True when a cached photo path is reconcile's synthetic missing-file key.

    A packet copies real bytes, so a true here means "do not copy, and say why"
    rather than "resolve it and let the copy fail with a path the human has
    never seen".
    """
    return path.startswith(_MISSING_PREFIX)


def _photo_people_paths(photos_conn: sqlite3.Connection, pid: str) -> set[str]:
    """
    Raw photo_people paths for pid - already the union of pid-keyword,
    face-tag, and name-match resolution (photoindex.py's _resolve_photo_people
    computes this once per scan; we just read it).
    """
    return {
        row['path']
        for row in photos_conn.execute(
            'SELECT DISTINCT path FROM photo_people WHERE person_ref = ?', (pid,)
        ).fetchall()
    }


def _expand_photo_groups(photos_conn: sqlite3.Connection, paths: set[str]) -> set[str]:
    """
    Expand a set of matched photo paths to every path sharing their
    group_id - so a person tagged on the front of a scan also gets its back
    and crop variants (TOOLING §9: a logical photo is the whole group, not
    one file). Paths with no group_id (shouldn't happen post-scan, but a
    stale/partial cache is possible) pass through unchanged.

    A 'MISSING:' member is expanded like any other, and a 'MISSING:' input
    still finds its group: a vanished front scan must still pull its back
    scan into the packet. Deciding which of the expanded paths can actually
    be copied happens at the copy site, not here.
    """
    if not paths:
        return set()
    placeholders = ','.join('?' * len(paths))
    group_ids = {
        row['group_id']
        for row in photos_conn.execute(
            f'SELECT DISTINCT group_id FROM photos WHERE path IN ({placeholders}) '
            f'AND group_id IS NOT NULL',
            list(paths),
        ).fetchall()
    }
    expanded = set(paths)
    if group_ids:
        gplaceholders = ','.join('?' * len(group_ids))
        for row in photos_conn.execute(
            f'SELECT path FROM photos WHERE group_id IN ({gplaceholders})', list(group_ids)
        ).fetchall():
            expanded.add(row['path'])
    return expanded


def _name_only_group_aliases(photos_conn: sqlite3.Connection, pid: str) -> set[str]:
    """
    Every cached photo path whose *logical photo* is tied to pid by name alone.

    "Matched by name only" is a fact about a physical photo, not about one scan
    of it: group expansion ships the back and the crop alongside a matched
    front, so the caution has to follow the group. It therefore survives when
    the name-matched variant is the one that has gone off disk and a sibling is
    what actually travels, and it lifts only when some stronger link - a P-id
    keyword or an exact face tag - verifies the same group.

    Returns alias keys with the 'MISSING:' ones left in, because a missing key
    is still evidence about its group; the caller counts only the files it
    managed to copy, which is what the recipient can actually look at.
    """
    name_matched: set[str] = set()
    verified: set[str] = set()
    for row in photos_conn.execute(
        'SELECT path, via FROM photo_people WHERE person_ref = ?', (pid,)
    ).fetchall():
        target = name_matched if row['via'] == 'name-match' else verified
        target.add(row['path'])
    return (
        _expand_photo_groups(photos_conn, name_matched)
        - _expand_photo_groups(photos_conn, verified)
    )


def _source_image_paths(
    source_files_by_id: dict[str, list[Path]],
) -> set[Path]:
    """Image-suffixed asset files among the included sources (gathering rule d)."""
    found: set[Path] = set()
    for paths in source_files_by_id.values():
        for p in paths:
            if _is_image_path(p):
                found.add(p)
    return found


# ── Claim-level restriction ────────────────────────────────────────────────────

def _source_copy_plan(
    conn: sqlite3.Connection,
    archive_root: Path,
    included_source_ids: list[str],
    *,
    include_restricted: bool,
    include_dna: bool,
) -> tuple[dict[str, str], set[str]]:
    """Decide how each included source's record file may be copied, and which
    claim ids the flags withhold from the generated timeline.

    A single sensitive `restricted:` claim can sit inside an unrestricted
    source (SPEC §8.4) - "cause of death: suicide", say - and the index
    carries no claim-level `restricted` column, so the marker is read from
    each included source's record file. Returns (copy_plan, timeline_excluded):
    copy_plan maps source_id to 'redact' (at least one claim is withheld under
    the active flags - copy through the line-span redactor) or 'unsafe' (the
    claims cannot be trusted at all - do not copy the record); a source absent
    from the plan is safe to byte-copy. `by-request` claims are withheld even
    with --include-restricted, like everywhere else.

    Withheld-ness never requires a claim id (round-2 finding 1: `id:` is
    optional on hand-written claims, and keying the withheld set by C-id let
    an id-less restricted claim ship verbatim). Ids matter only for the
    timeline exclusion set, and an id-less claim needs no entry there BY
    CONSTRUCTION: the timeline reads the index, and `fha index` drops any
    claim without a valid C-id, so the copied record file is the only surface
    an id-less claim can leak through - the copy is the leak surface, the
    timeline never sees them.

    Every parse doubt fails CLOSED as 'unsafe': read_record reporting
    parse_errors (its claims then read as [] - any number of restricted
    claims could be hiding in the text that would not parse), or a claims
    entry that is not a mapping (its `restricted:` flag cannot even be
    checked). The caller also keeps an 'unsafe' source's indexed claims out
    of the timeline: a fresh real index drops a malformed record's claims on
    its own, but the packet must not depend on that staying true of every
    index it is ever handed.

    This guard and the source-level one divide the record cleanly, and keeping
    them apart is what makes each proportionate. `_record_restriction` reads
    the FRONTMATTER for the source's own marker, and a failure there excludes
    the source outright - record, claims, assets, title. This one reads the
    CLAIMS block, and a failure here withholds the record and its claims while
    the asset files still ship: they carry no claim YAML, and the source-level
    marker was read and passed. A frontmatter failure therefore never reaches
    this function; a claims failure never reaches that one."""
    plan: dict[str, str] = {}
    timeline_excluded: set[str] = set()
    if not included_source_ids:
        return plan, timeline_excluded
    placeholders = ','.join('?' * len(included_source_ids))
    rows = conn.execute(
        f'SELECT id, path FROM sources WHERE id IN ({placeholders})', list(included_source_ids)
    ).fetchall()
    for row in rows:
        try:
            rec = read_record(archive_root / row['path'])
        except Exception:
            plan[row['id']] = 'unsafe'
            continue
        if rec['parse_errors']:
            plan[row['id']] = 'unsafe'
            continue
        for claim in rec['claims']:
            if not isinstance(claim, dict):
                plan[row['id']] = 'unsafe'
                break
            if not _restricted_included(
                claim.get('restricted'),
                include_restricted=include_restricted, include_dna=include_dna,
            ):
                plan[row['id']] = 'redact'
                cid = normalize_id(str(claim.get('id', '')))
                if cid:
                    timeline_excluded.add(cid)
    return plan, timeline_excluded


# ── Timeline ──────────────────────────────────────────────────────────────────

def _build_timeline_text(
    conn: sqlite3.Connection, pids: list[str], person_name: str,
    included_source_ids: set[str], excluded_claim_ids: set[str] | None = None,
) -> str:
    """
    Build a fresh timeline.md body for the packet.

    Filtered to `included_source_ids` so a claim sourced from a restricted/DNA
    record that was excluded from the packet doesn't leak its facts into the
    timeline anyway, and to `excluded_claim_ids` so a single restricted claim
    inside an otherwise-included source is withheld too. Intentionally simpler
    than `fha views timeline`'s decade grouping (no GENERATED header, no decade
    headers) - this is a one-shot export artifact, not a tracked archive view.

    A packet is family research material, not publication (the README says
    so), so needs-review claims stay IN - but tagged the same plain words the
    timeline views use: '[unconfirmed - parked {date}]' on a parked
    needs-review claim, '[low confidence]' on an accepted claim whose
    evidence is thin (owner decision 2026-07-22; the public site, by
    contrast, is accepted-only).

    pids carries the survivor plus any merged-away aliases (SPEC §9) so
    claims still attached to an old id still surface here.
    """
    excluded_claim_ids = excluded_claim_ids or set()
    if not included_source_ids:
        rows = []
    else:
        pid_placeholders = ','.join('?' * len(pids))
        src_placeholders = ','.join('?' * len(included_source_ids))
        rows = conn.execute(
            f"""
            SELECT DISTINCT c.id, c.date_edtf, c.date_min, c.type, c.value,
                   c.place_text, c.source_id, c.status, c.confidence, c.reviewed
            FROM claim_persons cp
            JOIN claims c ON cp.claim_id = c.id
            WHERE cp.person_id IN ({pid_placeholders}) AND c.status IN ('accepted', 'needs-review')
              AND c.source_id IN ({src_placeholders})
            ORDER BY
                CASE WHEN c.date_min IS NULL OR c.date_min = '' THEN 1 ELSE 0 END,
                c.date_min ASC
            """,
            list(pids) + list(included_source_ids),
        ).fetchall()
        rows = [r for r in rows if normalize_id(str(r['id'])) not in excluded_claim_ids]

    lines = [f'# Timeline: {person_name}\n']
    if not rows:
        lines.append('\n*(No claims from included sources.)*\n')
        return ''.join(lines)

    for row in rows:
        date_str = row['date_edtf'] or '(undated)'
        line = f'- {date_str} - {row["type"]}: {row["value"]}'
        if row['place_text']:
            line += f' @ {row["place_text"]}'
        line += f' [{fmt_id_display(row["source_id"])}]'
        if row['status'] == 'needs-review':
            line += f' [unconfirmed - parked {row["reviewed"]}]' if row['reviewed'] else ' [unconfirmed]'
        elif row['status'] == 'accepted' and row['confidence'] == 'low':
            line += ' [low confidence]'
        lines.append(line + '\n')
    return ''.join(lines)


# ── Packaging ─────────────────────────────────────────────────────────────────

def _unique_dest_path(dest_dir: Path, filename: str) -> Path:
    """Return a collision-free path for filename inside dest_dir.

    Two different sources rarely share a filename, but a stem-clash from
    same-named scans on different machines is possible - append ` (2)`, ` (3)`
    etc. rather than silently overwriting one file with another.
    """
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    n = 2
    while True:
        candidate = dest_dir / f'{stem} ({n}){suffix}'
        if not candidate.exists():
            return candidate
        n += 1


def _copy_into(src: Path, dest_dir: Path, *, messages: list[str] | None = None) -> Path | None:
    """
    Copy src into dest_dir, keeping its on-disk filename. None if src is gone
    or the copy itself failed.

    The copy is wrapped in try/except rather than left to propagate: a locked
    file, a permission error, or a full disk on ONE asset must not abort the
    whole packet build and must not exit 0 either - when `messages` is given,
    the failure is appended there so the caller's exit code reflects it
    (AGENTS_TOOLING.md: filesystem errors must affect exit status, never be
    silently swallowed).
    """
    if not src.exists():
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest_path(dest_dir, src.name)
    try:
        shutil.copy2(src, dest)
    except OSError as e:
        if messages is not None:
            messages.append(f'WARNING: could not copy {src}: {e}')
        return None
    return dest


def _plural_note(count: int, noun: str, filename: str) -> str:
    """One plain README line for withheld material, in the owner's language.

    "2 private facts were left out of x.md; they stay in your archive." - the
    non-technical reader must learn three things from one line: something was
    held back, how much, and that nothing was deleted from the archive itself."""
    if count == 1:
        return f'1 private {noun} was left out of {filename}; it stays in your archive.'
    return f'{count} private {noun}s were left out of {filename}; they stay in your archive.'


def _draft_note(count: int, filename: str) -> str:
    """One plain README line for withheld draft prose (round-2 S1).

    Same three lessons as _plural_note - something was held back, how much,
    nothing was deleted - plus the why in the owner's own terms: the
    paragraphs are waiting on his review (`fha confirm draft`), they are not
    private facts."""
    if count == 1:
        return (f'1 draft paragraph awaiting your review was left out of '
                f'{filename}; it stays in your archive.')
    return (f'{count} draft paragraphs awaiting your review were left out of '
            f'{filename}; they stay in your archive.')


def _asset_path_note(count: int, filename: str) -> str:
    """One plain README line for a `files:` entry redacted INSIDE a copied
    source record - not the missing-asset sentence `_redact_asset_path`
    already covers, the record's own frontmatter copy (issue #170 finding
    1, P1 follow-up; post-merge Codex review of #176, round-9 audit). Same
    three lessons as `_plural_note` - something was held back, how much,
    nothing was deleted - in the specific terms of a leaked local path
    rather than a private fact: the archive's own record is untouched,
    only the exported copy had the path swapped for a bare filename."""
    if count == 1:
        return (f'1 files: entry in {filename} named a path from your own computer, '
                f'not a portable archive alias, and was shown as a filename only in '
                f'the exported copy; your archive\'s own record is unaffected.')
    return (f'{count} files: entries in {filename} named paths from your own computer, '
            f'not portable archive aliases, and were shown as filenames only in the '
            f'exported copy; your archive\'s own record is unaffected.')


def _copy_redacted_source(
    src: Path,
    dest_dir: Path,
    *,
    include_restricted: bool,
    include_dna: bool,
    messages: list[str],
    redaction_notes: list[str],
) -> Path | None:
    """Copy a source record into the packet minus the claims the flags withhold.

    The unredacted record must never reach the packet, so unlike _copy_into
    every failure here fails CLOSED: an unreadable file, a Claims block whose
    entries cannot be matched to their lines (say, a hand-removed ```yaml
    fence - the forgiving reader still parses those claims, but line surgery
    on them is not safe), or a non-portable `files:` entry that cannot be
    safely rewritten (`_redact_source_record_files_field`, issue #170 finding
    1's P1 follow-up) SKIPS the copy with a warning naming the record and the
    fix. A missing record in a packet is recoverable; a leaked private fact
    or a leaked local path is not. The withhold decision itself lives in
    _redact_source_record_text, on the same parse that cuts, never keyed by
    claim id (round-2 finding 1). Successful redaction is quiet on stderr -
    it is the normal working of the privacy rules, not a problem - and speaks
    in the README."""
    try:
        text = read_text_exact(src)
    except (OSError, UnicodeError) as e:
        messages.append(
            f'WARNING: could not read {src}: {e} - the record was left out of sources/.'
        )
        return None
    files_redacted = _redact_source_record_files_field(text)
    if files_redacted is None:
        messages.append(
            f'WARNING: {src.name} names a files: entry outside the archive that could '
            'not be safely rewritten, so the record was left out of sources/ to be '
            'safe. It stays in your archive; run `fha lint` on it, then rebuild the '
            'packet.'
        )
        redaction_notes.append(
            f'{src.name} was left out of sources/: a files: entry named a path from '
            'your own computer that could not be safely rewritten. The record stays '
            'in your archive.'
        )
        return None
    text, asset_paths_redacted = files_redacted
    redacted = _redact_source_record_text(
        text, include_restricted=include_restricted, include_dna=include_dna,
    )
    if redacted is None:
        messages.append(
            f'WARNING: {src.name} holds private claims that could not be cleanly '
            'separated out, so the record was left out of sources/ to be safe. '
            'It stays in your archive; run `fha lint` on it, then rebuild the packet.'
        )
        redaction_notes.append(
            f'{src.name} was left out of sources/: it holds private facts that '
            'could not be separated out safely. The record stays in your archive.'
        )
        return None
    new_text, removed = redacted
    # #75: strip the visible purpose block before this copy leaves the
    # archive - scaffolding for the working archive, not content for the
    # family. Source records never carry #76's `## Sources` region or the
    # §16 person sections (only person profiles do), so
    # is_person_profile=False here.
    new_text = _strip_scaffolding_blocks(new_text, is_person_profile=False)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest_path(dest_dir, src.name)
    try:
        # Atomic here specifically, though the packet is regenerable output.
        # Every other write in the build raises into run_packet's cleanup
        # handler, which rmtree's the whole half-built directory - a
        # transaction stronger than any single-file guarantee, so those writes
        # need nothing. This one is the exception: its OSError is caught right
        # here and downgraded to a per-file WARNING, so the directory cleanup
        # never runs and the packet still ships as 'ok'. A truncating write
        # would leave a half-written source record sitting in sources/ that
        # the README lists as absent, and a relative opening the packet would
        # read it as the whole record. Fail closed means the file is either
        # complete or not there.
        write_text_exact_atomic(dest, new_text)
    except OSError as e:
        messages.append(f'WARNING: could not copy {src}: {e}')
        return None
    if removed:
        redaction_notes.append(_plural_note(removed, 'fact', dest.name))
    if asset_paths_redacted:
        redaction_notes.append(_asset_path_note(asset_paths_redacted, dest.name))
    return dest


def _copy_source_with_scaffolding_stripped(
    src: Path, dest_dir: Path, *,
    messages: list[str] | None = None,
    redaction_notes: list[str] | None = None,
) -> Path | None:
    """Copy a source record that needs no claim redaction, minus #75's
    purpose block - the byte-copy sibling of `_copy_redacted_source`.

    `_source_copy_plan` only routes a source through `_copy_redacted_source`
    when at least one of its claims is actually withheld under the active
    flags; every other included source (no claims, or claims but nothing
    restricted - the ordinary case) used to go straight to `_copy_into`
    untouched. That byte copy is still exactly right for the claims - there
    is nothing to cut - but it must not also ship the purpose block, so this
    is the same "read, strip, write only if it changed" shape
    `_strip_scaffolding_blocks`'s other two callers already use, not a
    reason to route this source through the claims redactor: `_redact_
    source_record_text` fails CLOSED (leaves the record out entirely) on a
    Claims block that is missing its ```yaml fence or absent altogether -
    a normal, lint-clean state for a source with nothing to claim yet - so
    sending every ordinary source through it would wrongly drop them from
    the packet instead of shipping them (minus one blockquote).

    Also runs `_redact_source_record_files_field` before the scaffolding
    strip (issue #170 finding 1, P1 follow-up, post-merge Codex review of
    #176 round-9 audit): this is the ONLY other place a source record's own
    text is copied into the packet, so a `files:` entry naming a real local
    path off the archive owner's machine must be rewritten here too, not
    just on the `_copy_redacted_source` branch - a source with no withheld
    claim is not exempt from that leak. This same call is also made on the
    research-file copy path (a research file's frontmatter never has a
    `files:` key, so it is a guaranteed no-op there, not dead weight).
    Fails CLOSED like `_copy_redacted_source` does, for the same reason: a
    files: entry that cannot be safely rewritten skips the copy entirely
    rather than ship it unredacted.

    Falls back to a plain `_copy_into` ONLY when the text WAS read and
    nothing needed stripping/redacting at all, which is the common case
    for a record untouched since before #75: no backfill/migration
    tooling means this is the ordinary shape for any pre-existing archive,
    not an edge case.

    Fails CLOSED - same as the files: redaction two paragraphs up, same
    warning shape as `_copy_redacted_source`'s own read failure - when the
    text canNOT be read at all (round-10 audit, issue #170 finding 1): a
    race, a permission problem, or a record that is not valid UTF-8
    (`read_text_exact` raises `UnicodeError`) all used to fall back to a
    plain `_copy_into` BYTE COPY of the untouched original - which means
    the files: redaction two paragraphs up never even ran, so a foreign
    `files:` entry in a record this function could not decode shipped
    completely unredacted. Skipping the copy instead is no worse for a
    genuinely clean record (it stays in the archive, the packet build
    exits with a warning instead of silently succeeding) and is the only
    safe choice for one that is not: the output can't be proven clean
    from a record that couldn't even be read to check."""
    try:
        text = read_text_exact(src)
    except (OSError, UnicodeError) as e:
        if messages is not None:
            messages.append(
                f'WARNING: could not read {src}: {e} - the record was left out of sources/.'
            )
        return None
    files_redacted = _redact_source_record_files_field(text)
    if files_redacted is None:
        if messages is not None:
            messages.append(
                f'WARNING: {src.name} names a files: entry outside the archive that '
                'could not be safely rewritten, so the record was left out of '
                'sources/ to be safe. It stays in your archive; run `fha lint` on '
                'it, then rebuild the packet.'
            )
        if redaction_notes is not None:
            redaction_notes.append(
                f'{src.name} was left out of sources/: a files: entry named a path '
                'from your own computer that could not be safely rewritten. The '
                'record stays in your archive.'
            )
        return None
    text, asset_paths_redacted = files_redacted
    new_text = _strip_scaffolding_blocks(text, is_person_profile=False)
    if new_text == text and not asset_paths_redacted:
        return _copy_into(src, dest_dir, messages=messages)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest_path(dest_dir, src.name)
    try:
        write_text_exact_atomic(dest, new_text)
    except OSError as e:
        if messages is not None:
            messages.append(f'WARNING: could not copy {src}: {e}')
        return None
    if asset_paths_redacted and redaction_notes is not None:
        redaction_notes.append(_asset_path_note(asset_paths_redacted, dest.name))
    return dest


def _write_readme(
    readme_path: Path,
    *,
    person_name: str,
    pid: str,
    included_sources: list[sqlite3.Row],
    excluded_sources: list[sqlite3.Row],
    unreadable_source_ids: set[str],
    other_named: list[sqlite3.Row],
    photo_count: int,
    unverified_photo_count: int,
    research_included: bool,
    research_draft_caution: bool,
    has_asset_files: bool,
    missing_assets: list[str],
    redaction_notes: list[str],
) -> None:
    lines = [
        f'fha packet - {person_name} ({fmt_id_display(pid)})\n',
        f'Generated {_today()}\n',
        '\n'
        'This is a derived export for family/private use - NOT a publication\n'
        'format, and not itself research data. Facts live in the family\n'
        'archive; this packet is a point-in-time copy of what the archive\n'
        'said about this person on the date above. Edits made here are not\n'
        'reflected back into the archive.\n',
    ]

    lines.append('\nContents:\n')
    lines.append('  profile/      person profile' + (' + research notes\n' if research_included else '\n'))
    lines.append('  timeline.md   chronological claims, generated fresh for this export\n')
    if included_sources:
        lines.append('  sources/      every included source record\n')
    if has_asset_files:
        lines.append('  files/        those sources\' asset files\n')
    lines.append(f'  photos/       {photo_count} photo file(s) of {person_name}\n')

    if unverified_photo_count:
        lines.append(
            f'\nNOTE: {unverified_photo_count} photo(s) in photos/ are matched by name only\n'
            'and have not been visually confirmed - treat as unverified.\n'
        )

    if research_included:
        # Research files ship as byte copies (round-2 scope decision: working
        # notes, not publication prose), so - unlike the profile and source
        # records - they are NOT run through the restricted-claim splice,
        # deadname strip, or draft withhold. Always say so, so a recipient
        # knows the research copy is raw notes that may name living or
        # restricted people; the second line pins the specific draft case when
        # a marker is actually present.
        lines.append(
            '\nNOTE: profile/ includes the raw research notes as working material\n'
            '(not publication-cleaned) - they may reference living or restricted\n'
            'people and are not redacted the way the profile and sources are.\n'
        )
        if research_draft_caution:
            lines.append(
                'They may also contain unreviewed draft text (AI-DRAFT sections\n'
                'awaiting review) - treat those as suggestions, not accepted facts.\n'
            )

    if included_sources:
        lines.append(f'\nIncluded sources ({len(included_sources)}):\n')
        for row in included_sources:
            lines.append(f'  [{fmt_id_display(row["id"])}] {row["title"]}\n')

    if excluded_sources:
        # Reason, not just a count: "restricted" over a record the tool could
        # not open would blame the wrong cause, and a README that misreports
        # why something is absent sends the owner looking in the wrong place.
        lines.append(
            f'\nExcluded sources ({len(excluded_sources)}) - restricted, DNA, or '
            'unreadable material withheld by default, listed by ID only:\n'
        )
        for row in excluded_sources:
            if row['id'] in unreadable_source_ids:
                reason = 'could not be read'
            elif row['source_type'] == 'dna':
                reason = 'DNA'
            else:
                reason = 'restricted'
            lines.append(f'  [{fmt_id_display(row["id"])}] ({reason})\n')

    if redaction_notes:
        lines.append('\nLeft out for privacy:\n')
        for note in redaction_notes:
            lines.append(f'  - {note}\n')

    if missing_assets:
        lines.append('\nMissing files (not copied):\n')
        for item in missing_assets:
            lines.append(f'  - {item}\n')

    if other_named:
        lines.append(
            f'\nCAUTION: this packet\'s materials name {len(other_named)} other living '
            'person(s). Handle accordingly before sharing further:\n'
        )
        for row in other_named:
            lines.append(f'  - {row["name"]} [{fmt_id_display(row["id"])}]\n')

    readme_path.write_text(''.join(lines), encoding='utf-8')


def _zip_directory(src_dir: Path, zip_path: Path) -> None:
    """Zip src_dir's contents into zip_path with paths relative to src_dir's parent
    (so the zip extracts back into a single top-level packet folder).

    Walked with `walk_files` and an error seam rather than `rglob`: this
    command just wrote every one of these files, so a subfolder that will not
    list means the zip would go out short of the sources a claim rests on
    while the run reported a packet built. A packet is handed to a relative
    who cannot check it against the archive - it has to be all there or not go
    at all - so the OSError raised here lands on `_packet_payload`'s
    write-failed arm, which removes the half-built folder and the partial zip
    and names the cause.
    """
    unreadable: list[Path] = []
    files = sorted(
        p for p in walk_files(src_dir, on_error=unreadable_dir_recorder(unreadable))
        if p.is_file()
    )
    if unreadable:
        shown = ', '.join(
            sorted(p.name for p in unreadable)[:5]) or str(src_dir)
        raise OSError(
            f'a folder inside the packet could not be read ({shown}), so the '
            f'zip would have been missing files without saying so - nothing '
            f'was kept. Check that folder (a drive or share that went away '
            f'mid-run is the usual cause), then run `fha packet` again'
        )
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, p.relative_to(src_dir.parent))


def _display_path(path: Path, archive_root: Path) -> str:
    """Return an archive-relative display path when possible, else absolute."""
    try:
        return str(path.relative_to(archive_root))
    except ValueError:
        return str(path)


# ── Core ──────────────────────────────────────────────────────────────────────

def _packet_payload(
    archive_root: Path,
    pid: str,
    out_dir: Path,
    *,
    include_research: bool = False,
    include_restricted: bool = False,
    include_dna: bool = False,
    no_photos: bool = False,
    dry_run: bool = False,
    overwrite: bool = False,
) -> dict:
    """
    Build a packet for pid under out_dir. Returns a result dict:

      {'status': 'ok'|'dry-run'|'not-found'|'not-curated'|'living-subject'|
       'restricted-subject'|'no-index'|'no-photoindex'|'output-exists'|
       'write-failed'|'bad-config'|'bad-output-path',
       'packet_dir': Path|None, 'zip_path': Path|None,
       'messages': [str, ...]}

    Strict index freshness is required because packet is a derived export
    whose privacy filters come from SQLite. Photoindex absence, unreadability,
    and staleness all block photo-bearing packets per TOOLING §8; --no-photos
    is the explicit escape hatch.

    fha.yaml is also loaded strictly: a malformed config must not be silently
    treated as {}, which would fall back external photos/documents roots to
    directories under the archive root and copy from (or report missing) the
    wrong files.

    out_dir is refused if it falls inside the archive root anywhere other
    than the top-level `out/` directory: a packet's copied .md records
    there would be picked up by a later `fha index` as if they were
    archive truth (TOOLING §15 "tools never import tools" applies just as
    much to one tool's output becoming another's input by accident).
    `out/` itself is exempt because `_index_citations` already skips it by
    the same rule - the two must agree on what's safe.
    """
    messages: list[str] = []
    try:
        resolved_out = out_dir.resolve()
        out_relative = resolved_out.relative_to(archive_root.resolve())
    except ValueError:
        out_relative = None
    if out_relative is not None and out_relative.parts and out_relative.parts[0] != 'out':
        return {
            'status': 'bad-output-path', 'packet_dir': None, 'zip_path': None,
            'messages': [
                f'ERROR: --out {out_dir} is inside {out_relative.parts[0]}/ - '
                'packet output must not be written into a record tree that '
                '`fha index` scans.'
            ],
        }
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        return {
            'status': 'bad-config', 'packet_dir': None, 'zip_path': None,
            'messages': [f'ERROR: {e}'],
        }

    conn = open_index_db(archive_root, _REQUIRED_TABLES, strict=True)
    if conn is None:
        return {'status': 'no-index', 'packet_dir': None, 'zip_path': None, 'messages': messages}

    try:
        person = _curated_person(conn, pid)
        if person is None:
            return {'status': 'not-found', 'packet_dir': None, 'zip_path': None, 'messages': messages}
        person, merge_notes = _resolve_merged_person(conn, person)
        if merge_notes:
            messages.extend(merge_notes)
        pid = person['id']
        if person['tier'] != 'curated':
            return {'status': 'not-curated', 'packet_dir': None, 'zip_path': None, 'messages': messages}
        if person['living'] in ('true', 'unknown'):
            return {
                'status': 'living-subject', 'packet_dir': None, 'zip_path': None,
                'messages': [
                    f'{fmt_id_display(pid)} has living={person["living"]}; '
                    'packet exports refuse living/unknown subjects by default.'
                ],
            }

        person_name = person['name']
        profile_path = archive_root / person['path']

        # A restricted subject is refused before any output: the packet would BE
        # this person's material. `by-request` is absolute; a plain/other type is
        # refused unless --include-restricted (dna unless --include-dna), the same
        # no-override rule every export path shares.
        #
        # A subject whose marker could not be READ is refused the same way, and
        # under every flag. The marker lives in the person's record file and
        # nowhere else - `persons` has no `restricted` column - so this decision
        # is made by reading a file, and a read that fails hands back exactly
        # what an unrestricted person hands back. It used to be taken as the
        # latter: the guard here was a bare `except Exception` around
        # `read_record(...)['meta'].get('restricted')`, and `read_record` does
        # not raise for the ordinary failures, so that arm caught almost
        # nothing - while a profile whose frontmatter block was gone entirely
        # raised nothing and reported nothing at all. That shipped a complete
        # zipped packet - profile, timeline, sources - for a person whose file
        # had said `restricted: by-request`.
        subject_restricted, subject_trouble = _record_restriction(profile_path)
        if subject_trouble is not None:
            return {
                'status': 'restricted-subject', 'packet_dir': None, 'zip_path': None,
                'messages': [
                    f'{fmt_id_display(pid)}: {person["path"]} could not be read '
                    f'({subject_trouble}), so there is no way to tell whether this '
                    'person asked to be left out of exports. Nothing was exported. '
                    'Repair that file (run `fha lint` to see what is wrong), run '
                    '`fha index`, then run `fha packet` again.'
                ],
            }
        if not _restricted_included(
            subject_restricted, include_restricted=include_restricted, include_dna=include_dna
        ):
            rtype = _restricted_type(subject_restricted)
            hint = (
                'this person asked to be left out (restricted: by-request) and is never exported.'
                if rtype == 'by-request' else
                f'this person is restricted ({rtype}); pass --include-restricted to build their packet.'
            )
            return {
                'status': 'restricted-subject', 'packet_dir': None, 'zip_path': None,
                'messages': [f'{fmt_id_display(pid)}: {hint}'],
            }

        photo_status = 'absent'
        if not no_photos:
            photo_status, _lag = photoindex_status(archive_root, fha_config)
            if photo_status in ('absent', 'unreadable', 'old-schema', 'stale'):
                return {
                    'status': 'no-photoindex', 'packet_dir': None, 'zip_path': None,
                    'messages': [
                        f'Photo index is {photo_status} - run `fha photoindex` first, '
                        'or pass --no-photos to export without photos.'
                    ],
                }

        alias_pids = [pid] + _merged_alias_ids(conn, pid)
        source_ids = _source_ids_for_person(conn, alias_pids)
        included_rows, excluded_rows, unreadable_source_ids = _classify_sources(
            conn, archive_root, source_ids,
            include_restricted=include_restricted, include_dna=include_dna,
            messages=messages,
        )
        included_ids = {r['id'] for r in included_rows}

        research_row = None
        if include_research:
            research_row = conn.execute(
                "SELECT path FROM person_files WHERE person_id = ? AND kind = 'research'",
                (pid,),
            ).fetchone()

        # Caution list combines structured-data matches now, and gets
        # extended with prose-citation and photo-only matches further below
        # - the dict stays open until just before the README is written so
        # every source can contribute without re-sorting repeatedly.
        copied_md_paths = {person['path']} | {r['path'] for r in included_rows}
        if research_row is not None:
            copied_md_paths.add(research_row['path'])
        other_named_by_id = {r['id']: r for r in _other_named_persons(conn, list(included_ids), pid)}
        for r in _citation_named_persons(conn, copied_md_paths, pid):
            other_named_by_id.setdefault(r['id'], r)

        files_by_source, missing_assets = _resolve_source_files(
            conn, archive_root, fha_config, list(included_ids)
        )
        for item in missing_assets:
            messages.append(f'WARNING: {item}')

        # A single restricted claim inside an otherwise-included source is
        # withheld from BOTH the generated timeline and the copied source
        # record itself (SPEC §8.4, TOOLING §8): the record is still shipped
        # (its other claims are fine), minus the withheld entries' YAML. A
        # record whose claims cannot be read safely is not shipped at all,
        # and none of its indexed claims reach the timeline - fail closed.
        copy_plan, excluded_claim_ids = _source_copy_plan(
            conn, archive_root, list(included_ids),
            include_restricted=include_restricted, include_dna=include_dna,
        )
        unsafe_source_ids = {sid for sid, mode in copy_plan.items() if mode == 'unsafe'}

        # A generational suffix (Jr, Sr, II, III, IV, V) is never the
        # surname (issue #78, the same #53 rule every other consumer
        # shares via `_lib.strip_generational_suffix`) - without an
        # indexed surname, the naive last-token fallback named the
        # deliverable `packet_jr_....zip`.
        #
        # A name with no tokens at all (a record whose `name:` is blank or
        # whitespace, filed under a stem with no `{surname}__` slot to read)
        # leaves nothing to fall back ON. The `or 'person'` below is the
        # answer to that, so this fallback has to be able to reach it
        # instead of raising IndexError on the way past.
        #
        # A packet filename is a naming surface - the human hands this zip
        # to a relative - so a placeholder is never allowed to name it
        # (`_lib.is_placeholder_name`, the same guard the couple-folder
        # names and the GEDCOM `1 NAME` line use). The indexed surname of
        # a person still filed as `unknown__unknown_P-….md` is the
        # title-cased slug "Unknown", which outlives the placeholder: a
        # human types a real name into that record and, until
        # `fha lint --fix-ids` renames the file, Roy Dodson's packet came
        # out as `packet_unknown_….zip`. Falling through to the name
        # gives him `packet_dodson_….zip`; a person with neither still
        # lands on the `or 'person'` default.
        _core, _ = strip_generational_suffix((person_name or '').split())
        indexed = person['surname'] or ''
        fallback = _core[-1] if _core else ''
        if is_placeholder_name(indexed):
            indexed = ''
        if is_placeholder_name(fallback):
            fallback = ''
        surname = indexed or fallback
        slug_surname = ''.join(c for c in surname.lower() if c.isalnum()) or 'person'
        packet_name = f'packet_{slug_surname}_{fmt_id_display(pid)}_{_today()}'
        packet_dir = out_dir / packet_name
        zip_path = out_dir / f'{packet_name}.zip'

        if packet_dir.exists() and not overwrite:
            return {
                'status': 'output-exists', 'packet_dir': packet_dir, 'zip_path': zip_path,
                'messages': [
                    f'Output already exists: {packet_dir}. '
                    'Pass --overwrite to replace this disposable packet output.'
                ],
            }
        if zip_path.exists() and not overwrite:
            return {
                'status': 'output-exists', 'packet_dir': packet_dir, 'zip_path': zip_path,
                'messages': [
                    f'Zip already exists: {zip_path}. '
                    'Pass --overwrite to replace this disposable packet output.'
                ],
            }

        if dry_run:
            return {
                'status': 'dry-run', 'packet_dir': packet_dir, 'zip_path': zip_path,
                'messages': messages,
            }

        redaction_notes: list[str] = []
        try:
            if packet_dir.exists():
                shutil.rmtree(packet_dir)
            if zip_path.exists():
                zip_path.unlink()
            packet_dir.mkdir(parents=True)

            # profile/ - the person's curated .md is the packet's central
            # record; a missing/failed copy is a structural failure (not the
            # per-file warning path used for optional assets), so it raises
            # into the cleanup handler below rather than shipping a packet
            # without it. The copy is checked for withheld name_variants
            # entries (private prior names) first, then for unaccepted
            # AI-draft prose; a profile that cannot be redacted - or whose
            # draft markers are too damaged to tell draft from accepted
            # prose - fails the build the same structural way, because the
            # packet can neither ship without a profile nor ship it verbatim.
            profile_dir = packet_dir / 'profile'
            profile_dir.mkdir()
            if not profile_path.exists():
                raise OSError(f'required profile file not found on disk: {profile_path}')
            try:
                profile_text = read_text_exact(profile_path)
            except (OSError, UnicodeError) as e:
                raise OSError(f'could not read required profile file: {e}')
            profile_redaction = _redact_profile_text(
                profile_text,
                include_restricted=include_restricted, include_dna=include_dna,
            )
            if profile_redaction is None:
                raise OSError(
                    f'private names in {profile_path.name} could not be separated '
                    'out of the copy - fix that file\'s frontmatter (`fha lint` '
                    'will point at the problem), then rebuild the packet.'
                )
            redacted_profile_text, hidden_name_count = profile_redaction
            profile_out_text, draft_count, draft_problem = _strip_profile_drafts(
                redacted_profile_text
            )
            if draft_problem is not None:
                raise OSError(
                    f'a draft marker in {profile_path.name} is damaged ({draft_problem}) - '
                    'unreviewed draft text cannot be told apart from accepted prose. '
                    'Repair the marker (usually: add the missing "-->"), or remove '
                    'the draft text, then rebuild the packet.'
                )
            # #75/#76: the purpose block and the `## Sources` region are
            # scaffolding for the working archive, not content for the
            # family - strip them the same way the draft markers just above
            # are stripped, before deciding whether this copy differs from
            # the original at all.
            profile_out_text = _strip_scaffolding_blocks(
                profile_out_text, is_person_profile=True
            )
            if profile_out_text != profile_text:
                # An OSError here raises into the cleanup handler at the bottom
                # of the build, which rmtree's the whole packet - so atomicity
                # is not what makes this write safe. It is atomic anyway
                # because there is no case where the truncating writer is
                # BETTER, only cases where it is merely sufficient, and one
                # writer for records is easier to keep right than two.
                write_text_exact_atomic(
                    _unique_dest_path(profile_dir, profile_path.name), profile_out_text,
                )
                if hidden_name_count:
                    redaction_notes.append(
                        _plural_note(hidden_name_count, 'name', profile_path.name)
                    )
                if draft_count:
                    redaction_notes.append(_draft_note(draft_count, profile_path.name))
            elif _copy_into(profile_path, profile_dir, messages=messages) is None:
                raise OSError(f'could not copy required profile file: {profile_path}')
            research_included = False
            research_draft_caution = False
            if include_research:
                research_path = archive_root / research_row['path'] if research_row else None
                if research_path is not None and research_path.exists():
                    # #75: a research file carries its own purpose block
                    # (`_lib.RESEARCH_PURPOSE_BLOCK`) exactly like a profile
                    # or a source record - scaffolding for the working
                    # archive, not content for the family - so it must be
                    # stripped here too. `_copy_source_with_scaffolding_
                    # stripped` already does exactly that (strip the purpose
                    # block only, plain byte-copy otherwise) with no claims
                    # redaction and no `## Sources` region to drop, which is
                    # exactly what a research file needs (still an
                    # UNREDACTED copy otherwise - see the caution below).
                    research_included = _copy_source_with_scaffolding_stripped(
                        research_path, profile_dir,
                        messages=messages, redaction_notes=redaction_notes,
                    ) is not None
                    if research_included:
                        # Research stays a byte copy (round-2 scope decision:
                        # working notes, not publication prose), so a draft
                        # marker inside travels with it - detect it for the
                        # README caution. A byte sniff, not a parse:
                        # 'AI-DRAFT' is ASCII, and an unreadable file just
                        # forgoes the caution it could not verify.
                        try:
                            research_draft_caution = b'AI-DRAFT' in research_path.read_bytes()
                        except OSError:
                            research_draft_caution = False
                elif research_path is not None:
                    messages.append(f'WARNING: research file not found on disk: {research_path}')
                else:
                    messages.append(
                        f'WARNING: --include-research requested but no research file is recorded for {fmt_id_display(pid)}.'
                    )

            # timeline.md - 'unsafe' sources are subtracted here as well as
            # skipped in the copy loop below: their privacy markers could not
            # be read, so none of their claims ship on ANY surface.
            (packet_dir / 'timeline.md').write_text(
                _build_timeline_text(
                    conn, alias_pids, person_name,
                    included_ids - unsafe_source_ids, excluded_claim_ids,
                ),
                encoding='utf-8',
            )

            # sources/ + files/ - a source whose Claims block holds withheld
            # claims gets a redacted copy; one whose claims could not be read
            # safely is left out entirely (fail closed); everything else is a
            # copy with #75's purpose block stripped (a byte copy if there
            # was none to strip). An 'unsafe' source's asset files still
            # ship: they carry no claim YAML, and the source itself passed
            # the source-level privacy gate.
            sources_dir = packet_dir / 'sources'
            files_dir = packet_dir / 'files'
            for row in included_rows:
                src_record = archive_root / row['path']
                if src_record.exists():
                    sources_dir.mkdir(exist_ok=True)
                    mode = copy_plan.get(row['id'])
                    if mode == 'unsafe':
                        messages.append(
                            f'WARNING: the claims in {src_record.name} could not be read, '
                            'so the record was left out of sources/ to be safe - a '
                            'private fact could be hiding in the part that would not '
                            'read. It stays in your archive; run `fha lint` on it, '
                            'then rebuild the packet.'
                        )
                        redaction_notes.append(
                            f'{src_record.name} was left out of sources/: its claims '
                            'could not be read, so private facts could not be ruled '
                            'out. The record stays in your archive.'
                        )
                    elif mode == 'redact':
                        _copy_redacted_source(
                            src_record, sources_dir,
                            include_restricted=include_restricted,
                            include_dna=include_dna,
                            messages=messages, redaction_notes=redaction_notes,
                        )
                    else:
                        # No claim here needs withholding, but #75's purpose
                        # block still does not belong in a shipped copy, and
                        # nor does a non-portable files: entry (issue #170
                        # finding 1, P1 follow-up).
                        _copy_source_with_scaffolding_stripped(
                            src_record, sources_dir,
                            messages=messages, redaction_notes=redaction_notes,
                        )
                else:
                    messages.append(f'WARNING: source record not found on disk: {src_record}')
                for asset_path in files_by_source.get(row['id'], []):
                    files_dir.mkdir(exist_ok=True)
                    _copy_into(asset_path, files_dir, messages=messages)

            # photos/
            photo_count = 0
            unverified_count = 0
            if not no_photos:
                photos_db = archive_root / '.cache' / 'photos.sqlite'
                pconn = sqlite3.connect(str(photos_db))
                pconn.row_factory = sqlite3.Row
                try:
                    people_paths = _photo_people_paths(pconn, pid)
                    # The README counts these as photos the recipient can look
                    # at ("N photo(s) in photos/ are matched by name only"), so
                    # the tally is taken at the copy site below, over files that
                    # actually landed in photos/. The name match itself belongs
                    # to the whole logical photo, so the caution has to travel
                    # with whichever variant ships: a group whose only link to
                    # this person is an unverified name match stays unverified
                    # even when the matched scan has gone off disk and only its
                    # back is left to copy.
                    name_only_aliases = _name_only_group_aliases(pconn, pid)

                    # Source-linked images aren't under photos/ control by tag, but a
                    # scan/copy of one may still share a photo_groups entry with a
                    # tagged photo (front/back/crop of the same physical item) - convert
                    # each to alias form and union with the tagged paths *before*
                    # expanding through photo_groups, so those siblings are captured too
                    # (TOOLING §9: a logical photo is the whole group, not one file).
                    # path_to_alias falls back to the absolute path's forward-slash form
                    # when the file isn't under the photos root at all; track those
                    # originals so they can still be copied directly.
                    source_alias_map: dict[str, Path] = {}
                    for src_image_path in _source_image_paths(files_by_source):
                        alias = path_to_alias(src_image_path, 'photos', fha_config, archive_root)
                        source_alias_map[alias] = src_image_path

                    combined_paths = set(people_paths) | set(source_alias_map)
                    expanded_aliases = _expand_photo_groups(pconn, combined_paths)

                    def _is_photo_alias(a: str) -> bool:
                        return a == 'photos' or a.startswith('photos/')

                    # A group can contain a photo reconcile has flagged as gone
                    # from disk. Its row is real and worth keeping (the caption
                    # and the tags on it are), but there are no bytes to put in
                    # the bundle, so it is separated out here and reported by
                    # name instead of being resolved into a path that could
                    # never open.
                    missing_aliases = {a for a in expanded_aliases if _is_missing_key(a)}
                    live_aliases = expanded_aliases - missing_aliases

                    # photo_people/photos store alias-form paths ('photos/…') that need
                    # resolve_path; a source image outside the photos root falls back to
                    # its own absolute path above and is used as-is. Keep the alias form
                    # alongside the resolved path so a "missing on disk" note can report
                    # it instead of a machine-specific absolute path when the photos
                    # root is mapped outside the archive.
                    photo_targets: dict[Path, str | None] = {}
                    for alias_path in live_aliases:
                        if _is_photo_alias(alias_path):
                            try:
                                resolved = resolve_path(alias_path, fha_config, archive_root)
                            except Exception:
                                continue
                            photo_targets[resolved] = alias_path
                        else:
                            photo_targets[source_alias_map.get(alias_path, Path(alias_path))] = None

                    # Photos that are in the catalog but not on disk: named in
                    # the README so the recipient knows the packet is short a
                    # picture on purpose, and flagged to whoever ran the export
                    # with the command that puts it back. Sorted so a packet
                    # built twice reads the same way both times.
                    for missing_key in sorted(missing_aliases):
                        note = (
                            'photo not on disk, so not copied: '
                            f'{_live_alias(missing_key)}'
                        )
                        messages.append(
                            f'WARNING: {note} - the photo catalog still remembers this '
                            'photo, but the file has moved or been deleted. Put it back, '
                            'then run fha photoindex reconcile --with-exif to re-link it.'
                        )
                        missing_assets.append(note)

                    copied_aliases: set[str] = set()
                    if photo_targets:
                        photos_dir = packet_dir / 'photos'
                        photos_dir.mkdir(exist_ok=True)
                        for abs_path in sorted(photo_targets, key=str):
                            alias_path = photo_targets[abs_path]
                            if not abs_path.exists():
                                display = alias_path or _display_path(abs_path, archive_root)
                                note = f'photo missing on disk: {display}'
                                messages.append(f'WARNING: {note}')
                                missing_assets.append(note)
                                continue
                            if _copy_into(abs_path, photos_dir, messages=messages):
                                photo_count += 1
                                if alias_path is not None:
                                    copied_aliases.add(alias_path)
                                if alias_path in name_only_aliases:
                                    unverified_count += 1

                    # A photo-group sibling may be tagged with a different,
                    # still-living/unknown person who never appears in any claim or
                    # source - catch that here so the caution list covers photo-only
                    # matches too. The list is built from what was actually copied,
                    # then re-expanded to those photos' groups: a person tagged only
                    # on a vanished front scan still belongs in the caution when the
                    # back scan of the same physical photo ships, while a photo that
                    # never made it into the bundle at all must not put a living
                    # person's name in the README for nothing.
                    tagged_aliases = {
                        a for a in _expand_photo_groups(pconn, copied_aliases)
                        if _is_photo_alias(_live_alias(a))
                    }
                    if tagged_aliases:
                        placeholders = ','.join('?' * len(tagged_aliases))
                        photo_person_ids = {
                            row['person_ref'] for row in pconn.execute(
                                f"SELECT DISTINCT person_ref FROM photo_people "
                                f"WHERE path IN ({placeholders}) AND person_ref != ?",
                                list(tagged_aliases) + [pid],
                            ).fetchall()
                        }
                        if photo_person_ids:
                            pplaceholders = ','.join('?' * len(photo_person_ids))
                            for row in conn.execute(
                                f"SELECT id, name FROM persons WHERE id IN ({pplaceholders}) "
                                f"AND living IN ('true', 'unknown')",
                                list(photo_person_ids),
                            ).fetchall():
                                other_named_by_id.setdefault(row['id'], row)
                finally:
                    pconn.close()

            # README.txt
            other_named = sorted(other_named_by_id.values(), key=lambda r: r['name'])
            _write_readme(
                packet_dir / 'README.txt',
                person_name=person_name, pid=pid,
                included_sources=included_rows, excluded_sources=excluded_rows,
                unreadable_source_ids=unreadable_source_ids,
                other_named=other_named, photo_count=photo_count,
                unverified_photo_count=unverified_count, research_included=research_included,
                research_draft_caution=research_draft_caution,
                has_asset_files=any(files_by_source.values()), missing_assets=missing_assets,
                redaction_notes=redaction_notes,
            )

            _zip_directory(packet_dir, zip_path)
        except (OSError, sqlite3.DatabaseError) as e:
            # A structural failure (can't create the packet dir, can't write
            # the zip, disk full mid-build, an incompatible photos.sqlite
            # schema) is different from one missing/locked file: it leaves
            # the build incomplete in a way per-file warnings can't express.
            # Clean up the half-built directory and any partial zip on a
            # best-effort basis (their own failure is swallowed - we're
            # already reporting the primary error) rather than leave debris
            # that would then block a retry with a misleading
            # "output already exists".
            try:
                if packet_dir.exists():
                    shutil.rmtree(packet_dir)
                if zip_path.exists():
                    zip_path.unlink()
            except OSError:
                pass
            messages.append(f'ERROR: packet build failed: {e}')
            return {
                'status': 'write-failed', 'packet_dir': None, 'zip_path': None,
                'messages': messages,
            }

        return {'status': 'ok', 'packet_dir': packet_dir, 'zip_path': zip_path, 'messages': messages}
    finally:
        conn.close()


def run_packet(
    archive_root: Path,
    pid: str,
    out_dir: Path,
    *,
    include_research: bool = False,
    include_restricted: bool = False,
    include_dna: bool = False,
    no_photos: bool = False,
    dry_run: bool = False,
    overwrite: bool = False,
) -> Result:
    """Build a person packet and return a Result.

    `data` is the `_packet_payload` dict ({'status', 'packet_dir', 'zip_path',
    'messages'}); Result exposes dict-style access (_lib.py), so callers keep
    reading `result['status']` / `result['packet_dir']` unchanged.  On a real
    build the written packet directory and zip are listed in `changed`; a
    --dry-run (status 'dry-run') writes nothing and leaves `changed` empty.
    """
    if is_working_copy(archive_root):
        _wc_msg = (
            'fha packet is not available in working-copy mode - '
            'the photo and document files are on the main machine. '
            'Run this command there.'
        )
        # Warning-level refusal, not a failure: ok stays True, exit stays clean,
        # data.status='working-copy' is the machine discriminator (TOOLING §13d).
        return Result(
            ok=True,
            exit_code=EXIT_CLEAN,
            data={'status': 'working-copy', 'packet_dir': None, 'zip_path': None,
                  'messages': [_wc_msg]},
        ).add('warning', _wc_msg)

    payload = _packet_payload(
        archive_root, pid, out_dir,
        include_research=include_research, include_restricted=include_restricted,
        include_dna=include_dna, no_photos=no_photos, dry_run=dry_run,
        overwrite=overwrite,
    )
    changed: list[str] = []
    if payload['status'] == 'ok':
        for key in ('packet_dir', 'zip_path'):
            value = payload.get(key)
            if value:
                changed.append(str(value))
    status = payload['status']
    # Map the payload status to the process exit code headless callers should
    # return.  `_cmd_packet` keeps its own per-status printing, but both paths
    # agree on the code: a clean/dry-run build that still emitted notes warns,
    # the soft "nothing built" statuses warn, and structural failures fail.
    if status in ('ok', 'dry-run'):
        exit_code = EXIT_WARNINGS if payload.get('messages') else EXIT_CLEAN
    elif status in ('not-found', 'not-curated'):
        exit_code = EXIT_WARNINGS
    else:  # no-index, bad-output-path, bad-config, living-subject,
           # restricted-subject, no-photoindex, output-exists, write-failed
        exit_code = EXIT_FAILURE
    return Result(
        ok=(status in ('ok', 'dry-run')),
        exit_code=exit_code,
        data=payload,
        changed=changed,
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

def _cmd_packet(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    if is_working_copy(archive_root):
        print(
            'fha packet is not available in working-copy mode - '
            'the photo and document files are on the main machine. '
            'Run this command there.',
            file=sys.stderr,
        )
        return EXIT_CLEAN

    pid = normalize_id(getattr(args, 'person_id', ''))
    if not pid:
        print('ERROR: a P-id argument is required.', file=sys.stderr)
        return EXIT_FAILURE

    out_dir = Path(getattr(args, 'out', None) or 'out')
    if not out_dir.is_absolute():
        out_dir = archive_root / out_dir

    result = run_packet(
        archive_root, pid, out_dir,
        include_research=getattr(args, 'include_research', False),
        include_restricted=getattr(args, 'include_restricted', False),
        include_dna=getattr(args, 'include_dna', False),
        no_photos=getattr(args, 'no_photos', False),
        dry_run=getattr(args, 'dry_run', False),
        overwrite=getattr(args, 'overwrite', False),
    )

    for m in result['messages']:
        print(m, file=sys.stderr)

    status = result['status']
    if status == 'no-index':
        return EXIT_FAILURE
    if status == 'bad-output-path':
        return EXIT_FAILURE
    if status == 'bad-config':
        return EXIT_FAILURE
    if status == 'not-found':
        print(f'{pid}: not found in index.', file=sys.stderr)
        return EXIT_WARNINGS
    if status == 'not-curated':
        print(f'{pid}: not a curated person - packets are only built for curated profiles.', file=sys.stderr)
        return EXIT_WARNINGS
    if status == 'living-subject':
        return EXIT_FAILURE
    if status == 'restricted-subject':
        return EXIT_FAILURE
    if status == 'no-photoindex':
        return EXIT_FAILURE
    if status == 'output-exists':
        return EXIT_FAILURE
    if status == 'write-failed':
        return EXIT_FAILURE
    if status == 'dry-run':
        print('(dry run - no changes written)')
        print(f'Would write: {_display_path(result["packet_dir"], archive_root)}')
        print(f'Would zip:   {_display_path(result["zip_path"], archive_root)}')
        return EXIT_WARNINGS if result['messages'] else EXIT_CLEAN

    print(f'Packet written: {_display_path(result["packet_dir"], archive_root)}')
    print(f'Zip:            {_display_path(result["zip_path"], archive_root)}')
    return EXIT_WARNINGS if result['messages'] else EXIT_CLEAN


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Bundle everything about one person into a zip to share with family.

  fha packet <P-id>              Build the packet (profile, timeline, sources, photos)
  fha packet <P-id> --dry-run    Preview what's included and what's withheld

A private family export, not a public website. Living people and restricted
material are withheld by default; opt in per export with the --include flags."""


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = subs.add_parser(
        'packet',
        help='Build a person export packet (profile, timeline, sources, files, photos) and zip it.',
        description=_CLI_DESCRIPTION,
    )
    p.add_argument('person_id', metavar='P-id', help='Curated person to export.')
    p.add_argument('-o', '--out', metavar='PATH', dest='out',
                    help="Output directory (default: 'out/' under the archive root).")
    p.add_argument('--include-research', action='store_true',
                    help="Include the person's research.md alongside the profile.")
    p.add_argument('--include-restricted', action='store_true',
                    help='Include restricted (non-DNA) sources. Excluded by default.')
    p.add_argument('--include-dna', action='store_true',
                    help='Include DNA sources. Excluded even with --include-restricted.')
    p.add_argument('--no-photos', action='store_true',
                    help='Skip photo gathering entirely (no photoindex required).')
    p.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='Preview the packet path and checks without writing files.')
    p.add_argument('--overwrite', action='store_true',
                    help='Replace an existing same-name packet directory/zip.')
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p.set_defaults(func=_cmd_packet)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha packet', description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('person_id', metavar='P-id', help='Curated person to export.')
    parser.add_argument('-o', '--out', metavar='PATH', dest='out',
                        help="Output directory (default: 'out/' under the archive root).")
    parser.add_argument('--include-research', action='store_true')
    parser.add_argument('--include-restricted', action='store_true')
    parser.add_argument('--include-dna', action='store_true')
    parser.add_argument('--no-photos', action='store_true')
    parser.add_argument('--dry-run', action='store_true', dest='dry_run')
    parser.add_argument('--overwrite', action='store_true')
    parser.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    parser.set_defaults(func=_cmd_packet)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
