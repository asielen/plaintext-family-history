#!/usr/bin/env python3
"""
views.py - fha views: generate view files from the index.

  fha views timeline [P-id | --all-curated] [--format md|html]
  fha views sources-index [P-id | --all-curated]
  fha views summary [P-id | --all-curated]
  fha views draft-queue [P-id | --all-curated] [--format md|html]
  fha views brackets [--fix | --fix-promote | --realign] [--generations N] [--dry-run] [--yes]
  fha views tree <P-id> --mode ancestors|descendants|fan [--generations N] [--format json|dot]
  fha views places [--dry-run]
  fha views clean [--dry-run]
  fha views refresh [--format md|html|both]

ARCHITECTURE OVERVIEW
---------------------
Three content views (timeline, sources-index, draft-queue) are read-only
projections derived from the index - they add no new facts.  Timeline and
draft-queue follow the same pipeline they always have:

  1. Open .cache/index.sqlite  (built by `fha index`; views never write it)
  2. Query claims / sources / persons tables
  3. Write one or more GENERATED-headed .md files back into the archive tree

The GENERATED header (see _gen_header) is the contract between views and lint:
lint rule W105 checks that generated companion files carry this exact header
so accidental hand-edits are caught on the next lint run.

Output files are "companion" files - they live alongside the profile they
describe and share its naming prefix, with a `view_` marker (#77) inserted
right before the kind word so the generated companions sort together and
AFTER the profile and the research file, instead of the disposable
draft-queue cache sorting first in the folder:
    hartley__thomas_edward_P-de957bcda1.md               ← profile (hand-edited)
    hartley__thomas_edward_research_P-de957bcda1.md      ← research (hand-edited, optional)
    hartley__thomas_edward_view_timeline_P-de957bcda1.md ← generated companion
    hartley__thomas_edward_view_draft-queue_P-de957bcda1.md

**Sources-index is different (#76).** The PER-PERSON form no longer writes a
separate companion file at all - it rewrites the `## Sources` section INSIDE
the person's own profile, as a GENERATED-BEGIN/END region (`_lib.
write_generated_region`), a section-scoped sibling of the whole-file GENERATED
header used above: same "machine-owned, do not hand-edit" contract, but
covering one section of an otherwise human-owned file instead of a whole
file. See `_lib.py`'s "GENERATED-BEGIN/END regions" block comment for exactly
how the two conventions differ and why a region is not a smaller version of a
companion file. There is no couple-folder sources index (SPEC 2.1 Part IV: a
couple folder's two profiles already list their sources) and no HTML twin -
there is nothing left to render standalone once the content lives inside the
profile; `--format html` refuses, naming `fha site` instead.
One more difference from timeline/draft-queue: sources-index works on a STUB
person too (`_generate_sources_index_person` skips the curated-only guard the
other two views keep), because #76's whole point is that a stub's `##
Sources` should already be populated by the time someone promotes it - not
regenerated from scratch afterward. `--all-curated` stays curated-only by its
own name; a single explicit `<P-id>` is not.

**Summary (SPEC §16)** is the profile's second region: `fha views summary`
writes the Born / Died / Married / Parents / Children lines between
`GENERATED-BEGIN summary` / `GENERATED-END summary` markers right under the
title and purpose block (`_lib.write_summary_region`), from accepted claims
only, on the same terms as sources-index (stubs too, re-sync, no HTML). It is
found by its markers, not a heading, and summary lines a human wrote with no
markers are theirs: never touched. With `views: completeness_line: true` in
fha.yaml (off by default) the region ends with one `**Records:**` line, the
person's completeness as `fha metrics` words it (`_lib.person_completeness`,
accepted claims). No export carries the region, so the line stays home.

Each content view also renders as a standalone single-file HTML page
(`--format html`), written under generated/views/ from the SAME queries and
the SAME line rendering as the .md twin: the HTML serializer re-renders the
finished .md lines (_md_inline_to_html), so the two formats cannot drift in
what they say.  The single-file-HTML conventions - shared by every fha
single-file artifact (the photo gallery inherits them; TOOLING §7 D11):

  - the GENERATED marker is line 1, BEFORE <!DOCTYPE html> (a comment before
    the doctype is valid HTML, no quirks mode); ownership is judged by
    _lib.is_generated_text exactly as for .md files;
  - one inline <style> block (design/view.css - a curated subset of the site
    stylesheet with a system font stack); no external requests of any kind;
  - a visible not-for-publication banner: views are private and UNREDACTED
    (SPEC §21 scope - views are not a public-output path); sharing goes
    through `fha site`;
  - [[ID]] tokens render as plain styled text (<span class="cite">), never
    links - a standalone file has nothing durable to link to;
  - writes under generated/ touch nothing the indexer scans, so HTML runs
    never stale the index (exit 0 clean, no reindex advice).

**Purpose blocks (SPEC §16a, #75).** Every file this module writes - the two
remaining companion kinds and their HTML twins - opens with a short VISIBLE
blockquote (`_lib.VIEW_PURPOSE_BLOCKS`)
right after its title, in addition to (never instead of) the GENERATED
header: the header is the machine-readable signal `fha views clean` reads;
the blockquote is for a human reading the file in Obsidian preview or on
GitHub, where an HTML comment is invisible. `fha site`/`fha packet` strip it
before publication (see `packet.py`'s scaffolding stripper) - it is
scaffolding for the working archive, not content for the family.

**Place notes (SPEC §15)** exist only in Obsidian link mode (`links:
obsidian` in fha.yaml): `fha views places` writes one GENERATED note per
registered place, `places/{slug}_{l-id}.md`, so a place link has a file
Obsidian can follow. Unlike a companion, the note opens with frontmatter
(`aliases:`, which Obsidian reads only at the top of a file), so its marker
is the first line AFTER the frontmatter and ownership is judged there
(`after_frontmatter=True`). A rename in places.yaml removes the old note and
re-points file-name links to it (`_lib.rewrite_stale_stem_links`). `refresh`
runs the same pass first in that mode; `clean` sweeps the notes. In `id`
mode nothing is written.

`fha views brackets` is a maintenance view, not a content view.  It reads
the index to derive expected bracket lists and Ahnentafel positions, then
reports mismatches as W103/W110 findings - and direct-line ancestors still
filed as stubs as W119 findings (leads, never defects), plus W120 notes for
slots the derivation had to default because a lone linked parent has no
recorded sex: (report-only; the human records the fact).  With --fix it
renames folders and moves person files; with --fix-promote it batch-applies
the shared promote engine (`_lib.promote_person_record` - the same engine
behind `fha person promote`) to the W119 set under the previewed Apply?
gate; with --realign it does both halves in one combined preview under one
gate (promotion destinations rebased through the pending renames) - the
one-command recovery whenever the derived tree drifts from the promoted
folders; --dry-run previews all changes without writing.

SPEC §16 defines the companion kinds; TOOLING §7 defines each view's content.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import deque
from html import escape as html_escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    claim_date_order_sql,
    PARKED_CLAIM_SQL,
    AmbiguousCoupleFolderError,  # two couple folders share a prefix; refuse, don't guess,
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FhaConfigError,
    GeneratedFileParentMissing,  # a companion's parent folder is missing (stale index),
    GeneratedFileRefused,    # shared refusal when a write would clobber a non-generated file,
    PromotionError,       # a --fix-promote promotion refused/failed (rolled back),
    Result,               # the structured-result contract every run_* returns,
    SOCIAL_PARENT_SUBTYPES,  # natures never numbered into the pedigree (SPEC §12.2),
    VIEW_PURPOSE_BLOCKS,  # the visible "do not edit" blockquote per view kind (SPEC §16a, #75),
    ahnentafel_generation,   # Ahnentafel position → generation depth (--generations cap),
    archive_title,        # masthead/page title from fha.yaml site.archive_name,
    build_ahnentafel_map,    # index BFS {P-id → position}; shared with fha person promote,
    claim_is_own_vital,   # is this vital claim a record OF this person? (roles:, #126),
    couple_folder_dirs,   # digit-prefixed dirs under people/ (not stubs/connections),
    couple_folder_for_prefix,   # canonical on-disk couple folder for a number, or None,
    couple_folder_prefix,    # position → the even couple-folder number,
    link_parts,
    claim_source_lookup,
    cited_source_ids,
    find_person_record_path,   # disk-truth record lookup for --fix-promote applies,
    fmt_id_display,       # the one ID form tools write and show: all lowercase,
    bracket_given_name,      # default child token, title-aware - shared with lint W103,
    reconcile_bracket_entries,   # keeps a still-true hand-written child token (W103),
    format_w120_message,  # the W120 wording - shared with lint so the twins agree,
    is_generated_text,    # GENERATED-header ownership test (first non-blank line),
    is_genetic_parent_subtype,
    is_placeholder_name,  # 'unknown'/'None'/'' - never written into a folder name,
    folder_name_text,     # a name made safe for a folder name on every OS,
    load_fha_yaml,
    load_view_css,        # cached design/view.css loader -> (css, warning_or_None),
    nonbirth_bracket_label,  # 'adopted'/'step'/… mark for a non-birth child,
    normalize_id,         # lower-cases IDs for consistent set/dict keying,
    open_index_db,        # open .cache/index.sqlite with freshness check + table probe,
    promote_person_record,   # the ONE promote engine (tier flip + body-section backfill + move),
    read_record,          # parses YAML front-matter + body from a .md file,
    read_text_exact,      # newline-exact record read (no CRLF/LF translation),
    reapply_newline,      # restore a record's CRLF/LF convention after a text edit,
    render_template,      # load + render a tools/templates/ Jinja2 template (shared),
    resync_person_profile_rows,  # keep notes_fts current after an in-place ## Sources
                             # edit without staling the freshness watermark (#76),
    section_bounds,       # locate one `## Heading` prose section's line span,
    pip_command,
    requirements_hint,
    resolve_root_arg,      # --root flag, else find_archive_root(), shared error message,
    resolve_root_generation,   # validate fha.yaml root_generation: 'self'|'children' (#72),
    root_generation_seed_position,  # 'self'|'children' -> the Ahnentafel seed position,
    spouse_extended_base,    # add-only `+ second spouse` folder-name rule - shared with lint W103,
    sync_generated_view_rows,   # keep notes_fts/person_files in step with view writes/deletes,
    unreadable_dir_recorder,  # os.walk error seam - a shut folder is not an empty one,
    vital_pick_key,       # which accepted vital is the node label: most exact first (#201),
    walk_files,           # rglob replacement that HAS that error seam,
    write_generated_file,    # marker-guarded write shared with photoindex gallery,
    write_generated_region,  # rewrite a `## Heading` section as one GENERATED-BEGIN/END
                             # region, in place or newly created (SPEC §21b, #76),
    write_summary_region,  # the profile's Born/Died/... summary region (SPEC §16),
    completeness_context,  # the completeness engine's index read (TOOLING §7a),
    person_completeness,   # one person's five checks - the opt-in Records line,
    completeness_summary_line,  # those checks as one plain line,
    strip_generated_region,  # cut one named region out of a profile body,
    SUMMARY_REGION,       # that region's name,
    humanize_edtf,        # an EDTF date as a reader says it ('3 March 1840'),
    link_writer,          # the archive's link form (SPEC §18 link mode)
    link_mode,            # 'id' | 'obsidian' from fha.yaml's links:
    LinkWriter,
    generated_text_differs,   # the date-masked "did anything change" test,
    is_generated_file,
    is_place_note_path,   # places/{slug}_{l-id}.md, the place-note name (SPEC §15),
    is_owned_place_note,  # a place note views places wrote (marker after frontmatter),
    is_template_file,
    place_note_stem,      # a place's note stem from its id and name,
    rewrite_stale_stem_links,  # re-point file-name links after a place rename,
    strip_link_wrapper,
    rewrite_record_links_to_stems,
    link_entry,           # `[[p-…|Name]]` - the one written form of a person link,
    export_privacy,       # what an export must withhold, read once (printed reports),
    export_tie_ok,        # may a walk over the family use this tie (D16),
    person_restricted,    # asked to be left out (any `restricted:` value),
    person_withheld,      # living, unknown or restricted: not named in an export,
    read_restriction_marker,  # why a record's privacy marker would not read,
    public_source_sql,    # the sources an export may use,
    merge_chain,
    merge_survivor,
    write_text_exact_atomic,  # atomic record writer: temp file + fsync + os.replace,
)


def _views_result(
    exit_code: int,
    *,
    changed: list[str] | None = None,
    data: dict | None = None,
) -> Result:
    """Build a views Result.

    The view commands generate/delete companion files (side effects) and narrate
    their progress as they go; that printing stays inline (the prompts in
    `run_brackets` make it inseparable), so the Result records the outcome - the
    exit code, whether it succeeded, and the files written/removed in `changed` -
    rather than re-deriving printed lines.  `ok` is true for clean and
    warnings-only runs (a generated-but-now-stale-index run still succeeded).
    """
    return Result(
        ok=(exit_code not in (EXIT_ERRORS, EXIT_FAILURE)),
        exit_code=exit_code,
        changed=changed or [],
        data=data or {},
    )

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Helpers (shared utilities)
#    _today, _gen_header          - GENERATED header text
#    (database / root resolution now live in _lib.py: open_index_db, resolve_root_arg)
#    _rel_display                 - archive-relative path for user-facing lines
#    _sync_index_rows             - keep notes_fts/person_files in step with view writes
#    _sync_written_views          - the above for a write path, plus the right advice line
#    _batch_view_result / _single_view_result - the two halves' shared exit-code rule
#    _profile_path_for            - locate a person's .md profile file
#    _out_path_for                - build companion file path from profile path
#    _format_sid, _place_label    - formatting helpers
#    _curated_person_ids          - list all curated P-ids from index
#
#  HTML single-file rendering (--format html; conventions in TOOLING §7 D11)
#    _md_inline_to_html           - escape + [[ID]] tokens → styled <span> text
#    _html_out_path               - generated/views/{stem}.html target path
#    _format_precheck             - engine-level fmt validation + Jinja2 probe
#    _view_css                    - cached design/view.css for inlining
#    _render_view_html            - wrap a body in the standalone page shell
#    _timeline_body_html          - timeline sections → HTML body
#
#  Timeline view  (_generate_timeline)
#    _decade_from_edtf            - EDTF string → '### 1880s' decade header
#    _timeline_claim_line         - format one claim as a timeline bullet
#    _generate_timeline           - main generator: queries, groups, writes file
#
#  Sources-index view  (_generate_sources_index_*)
#    _source_ids_for_persons      - collect all source IDs linked to a person set
#    _source_rows_by_type         - source rows for a set of ids, grouped by source_type
#    _sources_index_region_body   - the grouped listing rendered for the per-person REGION
#                                 (nested under an existing ## Sources heading, so plain
#                                 bold labels, not a second level of ## headings)
#    _generate_sources_index_person        - per-person: rewrites the profile's own
#                                 `## Sources` GENERATED-BEGIN/END region in place (#76) -
#                                 no separate file, works on a stub as well as a curated person
#
#  Summary region  (_generate_summary_person, run_summary)
#    _summary_region_body         - Born/Died/Married/Parents/Children lines from accepted
#                                 claims; _summary_own_vital / _summary_kin pick the facts
#    _summary_completeness_context / _summary_records_line - the opt-in Records line
#                                 (fha.yaml `views: completeness_line: true`), from the
#                                 shared completeness engine `fha metrics` uses
#    _generate_summary_person     - writes the profile's summary region in place
#                                 (`_lib.write_summary_region`); hand-written lines untouched
#
#  Draft-queue view  (_generate_draft_queue)
#    _generate_draft_queue        - diff accepted sources against cited tokens
#
#  Brackets view  (_cmd_brackets and helpers)
#    _parse_bracket_names         - extract [Name1 + Name2] list from folder name
#    _folder_numeric_prefix       - '040 Thomas …' → 40
#    _couple_folder_dirs          - list digit-prefixed dirs under people/
#    _persons_in_folder           - person_ids whose profile files live in a folder
#    _build_ahnentafel_map        - BFS from root_person → {pid: int position}
#                                    (+ W120 sex-gap collection via _lib);
#                                    root_position seeds #1 ('self') or #2
#                                    ('children' - root_generation:, issue #72)
#    _person_label_from_db        - `Ada Hartley (P-…)`, or the ID alone if nameless
#    _check_w127_root_anchor      - W127: root_person has a child, so the walk starts
#                                    a generation too high (lint twin: lint.py);
#                                    caller skips this entirely when
#                                    root_generation: children says that is deliberate
#    _check_w103_brackets         - derive expected bracket lists + the missing
#                                    `+ spouse` half, find mismatches
#    _person_couple_folder        - locate a person's current couple folder via index
#    _person_name_from_db         - fetch display name from persons table
#    _companion_files_in_folder   - disk-scan for all .md files for a person in a folder
#    _check_w110_ahnentafel       - check 2 (folder rename) and check 3 (file move)
#    _check_w119_direct_line_stubs - check 4: direct-line ancestors still filed as stubs
#    _print_promote_preview       - plain-words promotion plans (engine dry runs)
#    _apply_promotions            - batch-apply _lib.promote_person_record to the W119 set
#    _realign_promotion_dests     - re-point W119 dests at the post-rename tree (--realign)
#    _compose_folder_renames      - merge W103+W110 renames that share a source folder
#    _print_bracket_preview       - format the preview diff before any writes
#    _planned_folder_renames      - every folder rename the pass will perform
#    _location_before_renames     - a post-rename path read back onto today's tree
#    _bracket_fix_conflicts       - the ONE preflight: taken folder AND file destinations
#    _apply_bracket_fixes         - perform renames/moves after confirmation
#    _drop_index_cache            - the shared post-fix cache drop + its failure message
#    _print_fix_conflicts         - refuse a blocked plan before the Apply? gate
#    _cmd_brackets                - CLI handler: report, preview, fix, fix-promote, realign
#
#  Tree view  (_cmd_tree and helpers)
#    _build_nodes_bulk            - batch TOOLING §7 node dicts for all BFS pids (2 SQL queries)
#    _collect_edges               - DISTINCT edges from relationships table for a pid
#    _traverse_tree               - BFS with cycle detection; returns nodes + edges dicts
#    _edge_to_json_dict           - edge dict → TOOLING §7 JSON edge schema
#    _tree_to_json                - serialize traversal to neutral JSON (TOOLING §7 D3)
#    _tree_to_dot                 - serialize traversal to GraphViz DOT
#    _cmd_tree                    - CLI handler: traversal + output
#
#  Place notes  (run_places; Obsidian link mode only, SPEC §15)
#    _registered_places           - places + alt names + history from the index
#    _place_note_text             - one note: frontmatter aliases, header, registry facts
#    _owned_place_note            - views marker on the first line after the frontmatter
#    _place_record_files          - records whose file-name place links follow a rename
#    _place_notes_pass            - remove stale notes, write notes, re-point links
#    _place_notes_summary         - the closing lines
#    run_places, _cmd_places      - engine and CLI handler
#
#  CLI wiring
#    _cmd_timeline, _cmd_sources_index, _cmd_draft_queue  - argparse handlers
#    _cmd_brackets                - brackets handler (above)
#    _cmd_tree                    - tree handler (above)
#    _cmd_clean                   - delete all GENERATED companion files
#    _cmd_refresh                 - regenerate all content views for all curated persons
#    _cmd_views_help              - prints views help when no subcommand given
#    register           - called by fha.py to attach 'views' to the main parser
#    register_standalone, main    - used when running `python tools/views.py` directly
#
# ─────────────────────────────────────────────────────────────────────────────


# ── Helpers ───────────────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.date.today().isoformat()


# The first non-blank line of a generated view file always begins with this
# marker.  It is the contract between views (writer), lint (W105), and
# `views clean` (deleter): a file is "owned" by views only when this marker is
# its first non-blank line - never merely present somewhere in the body.
_GEN_MARKER = '<!-- GENERATED by fha views'


def _gen_header(subcommand: str) -> str:
    """
    Return the GENERATED header comment for a view file.

    The exact wording 'GENERATED by fha views {subcommand}' is checked by lint
    rule W105 - if you change this string, update the W105 pattern in lint.py
    as well, otherwise every existing generated file will be flagged as
    hand-edited on the next lint run.
    """
    return (
        f'{_GEN_MARKER} {subcommand} on {_today()}'
        ' - do not edit; regenerate instead -->\n\n'
    )


# A view's purpose block carries exactly one **bold** lead-in (SPEC §16a,
# #75's own wording, e.g. "**Generated view - do not edit.**") - fixed
# wording this module controls, not arbitrary user prose, so a tiny
# dedicated conversion is enough; stretching `_md_inline_to_html`'s
# [[ID]]-only contract to also understand markdown bold would risk it firing
# on a claim value or title that happens to contain a literal `**`.
_BOLD_MD_RE = re.compile(r'\*\*(.+?)\*\*')


def _purpose_block_md(subcommand: str) -> str:
    """The visible purpose blockquote (SPEC §16a, #75) for one view kind, as
    markdown ready to drop right after an H1 that ends in a single '\\n'.

    Supplies its OWN leading blank line (so the caller's H1 chunk never has
    to know whether a block follows) and ends in exactly one '\\n' - matching
    this file's existing convention of each appended chunk supplying its own
    leading separator (see `_generate_timeline`'s decade loop), so whatever
    content chunk follows next needs no adjustment either. Empty string when
    the kind carries no block (keeps every call site unconditional rather
    than branching on presence).
    """
    block = VIEW_PURPOSE_BLOCKS.get(subcommand)
    return f'\n{block}\n' if block else ''


def _purpose_block_html(subcommand: str) -> str:
    """The same purpose block, escaped and wrapped as a `<blockquote>` for a
    standalone HTML view - the doctype-safe sibling of `_purpose_block_md`."""
    block = VIEW_PURPOSE_BLOCKS.get(subcommand)
    if not block:
        return ''
    text = ' '.join(ln.lstrip('>').strip() for ln in block.split('\n') if ln.strip())
    bolded = _BOLD_MD_RE.sub(r'<strong>\1</strong>', html_escape(text))
    return f'<blockquote>{bolded}</blockquote>\n'


# One link writer per views run (SPEC §18 link mode): it reads the file names
# once, not once per person. Each run_* clears it first, so a long-lived front
# door (fha serve) never writes a file name a rename has since changed.
_VIEW_LINKS: dict[Path, LinkWriter] = {}


def _view_links(archive_root: Path) -> LinkWriter:
    """The run's `LinkWriter` for this archive (see `_VIEW_LINKS`)."""
    key = Path(archive_root)
    if key not in _VIEW_LINKS:
        _VIEW_LINKS[key] = link_writer(key)
    return _VIEW_LINKS[key]


def _write_view_file(out_path: Path, content: str, links: LinkWriter | None = None) -> Path:
    """Write a generated view file, refusing to clobber hand-written content.

    `links` (the markdown views only) writes the view's person, source and
    claim links in the archive's link form (SPEC §18): the file-name form
    when fha.yaml says `links: obsidian`, unchanged otherwise.

    Thin wrapper over the shared _lib.write_generated_file guard (the ownership
    rule lives there so views and the photoindex gallery cannot drift): raises
    GeneratedFileRefused if a file already exists at out_path whose first
    non-blank line is not the views GENERATED marker. The narrower views prefix
    keeps another tool's GENERATED file protected from a views overwrite.
    """
    if links is not None:
        content = rewrite_record_links_to_stems(content, links)[0]
    return write_generated_file(out_path, content, _GEN_MARKER)


def _refused_exit(e: GeneratedFileRefused) -> int:
    """Report a refused overwrite and return the failure exit code."""
    print(
        f'ERROR: refusing to overwrite hand-written file (no GENERATED header): '
        f'{e} - move or delete it, then re-run.',
        file=sys.stderr,
    )
    return EXIT_FAILURE


def _missing_parent_exit(e: GeneratedFileParentMissing) -> int:
    """Report a companion write whose person folder no longer exists.

    write_generated_file raises this instead of recreating the folder (a
    stale .cache/index.sqlite can point at a folder that moved or was
    deleted) - never silently rebuild archive structure from cache state.
    """
    print(
        f'ERROR: the folder for {e} no longer exists on disk - it may have '
        f'moved or been deleted since the index was last built. Run `fha index` '
        f'to refresh the index, then try again (or restore the folder).',
        file=sys.stderr,
    )
    return EXIT_FAILURE


def _write_os_error_exit(e: OSError) -> int:
    """Report a failed view write (permission denied, disk full, a bad --out
    target) and return the failure exit code, instead of a raw traceback."""
    print(
        f'ERROR: could not write the view file ({e.strerror or e}). Check that '
        f'the destination folder exists and is writable (or that the disk has '
        f'room), then re-run.',
        file=sys.stderr,
    )
    return EXIT_FAILURE


def _runtime_error_exit(e: RuntimeError) -> int:
    """Report an engine-layer RuntimeError (e.g. _lib.render_template's
    translated broken-template message) and return the failure exit code."""
    print(f'ERROR: {e}', file=sys.stderr)
    return EXIT_FAILURE


def _generate_or_warn(context: str, fn, *args, **kwargs):
    """Call a per-item view generator inside a batch (--all-curated /
    refresh), warning and returning None instead of letting a single item's
    write failure abort the rest of the batch.

    Before this wrapper, GeneratedFileParentMissing (routine: one curated
    person's folder is stale/moved relative to the index) hit the same
    top-level except as the rare GeneratedFileRefused case and aborted the
    whole run, discarding `changed` for every person already written this
    pass. `context` names what's being skipped, e.g. 'timeline for P-xxx'.
    A single-person (non-batch) call is NOT expected to use this wrapper -
    there the top-level except clauses below are correct, since one item IS
    the whole run.
    """
    try:
        return fn(*args, **kwargs)
    except GeneratedFileRefused as e:
        print(
            f'WARNING: skipped {context} - hand-written file at {e} has no '
            f'GENERATED header (move or delete it, then re-run for this one).',
            file=sys.stderr,
        )
    except GeneratedFileParentMissing as e:
        print(
            f'WARNING: skipped {context} - the folder for {e} no longer '
            f'exists (run `fha index` to refresh, then retry this one).',
            file=sys.stderr,
        )
    except OSError as e:
        print(f'WARNING: skipped {context} - could not write ({e.strerror or e}).',
              file=sys.stderr)
    except RuntimeError as e:
        print(f'WARNING: skipped {context} - {e}', file=sys.stderr)
    return None


def _batch_view_result(
    count: int, skipped: int, changed: list[str], what: str,
    index_sync: str = 'indexed',
) -> Result:
    """The Result a bulk view run returns, warning when anything was skipped.

    Every skip already printed its own WARNING with the reason and the fix (a
    hand-written file in the way, a folder the index no longer finds). What was
    missing is the exit code: a batch that wrote nine of ten files exited 0, so
    a harness - or a human reading only the last line - could not tell the run
    was incomplete, while the SAME condition on a single-person run exits 1.

    `index_sync` is the outcome of the companions' index-row update, and
    'index_error' warns for the same reason a skip does: the files landed, but
    the archive is not in the state the command promised. Generated companions
    are outside the freshness watermark (#37), so a failed row update is
    invisible afterwards - index.sqlite keeps looking current while `notes_fts`
    serves the previous view's words. It is reported as `index_stale`, the key
    `brackets` already uses when a cache step fails after the disk work landed.
    """
    data: dict = {'count': count}
    if skipped:
        print(
            f'{skipped} {what} file(s) skipped - see the warnings above; '
            'fix what each one names and re-run for those people.'
        )
        data['skipped'] = skipped
    if index_sync == 'index_error':
        data['index_stale'] = True
    if skipped or index_sync == 'index_error':
        return _views_result(EXIT_WARNINGS, changed=changed, data=data)
    return _views_result(EXIT_CLEAN, changed=changed, data=data)


def _single_view_result(index_sync: str, changed: list[str]) -> Result:
    """The Result one person's view write returns; the twin of `_batch_view_result`.

    Split out so the two halves cannot drift on the exit code for the same
    condition: a companion that landed while its index row did not is an
    incomplete run whether one person or forty were generated.
    """
    data: dict = {'count': 1}
    if index_sync == 'index_error':
        data['index_stale'] = True
        return _views_result(EXIT_WARNINGS, changed=changed, data=data)
    return _views_result(EXIT_CLEAN, changed=changed, data=data)


def _rebase(p: Path, old: Path, new: Path) -> Path:
    """Return p relocated from old to new, or p unchanged if not under old."""
    try:
        return new / p.relative_to(old)
    except ValueError:
        return p


def _rel_display(p: Path, archive_root: Path) -> str:
    """The archive-relative form of a path for user-facing lines, or the
    absolute path when it lives outside the archive (an external root)."""
    try:
        return str(p.relative_to(archive_root))
    except ValueError:
        return str(p)


def _sync_index_rows(
    archive_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
    written: list[Path] | tuple[Path, ...] = (),
    removed: list[Path] | tuple[Path, ...] = (),
) -> str:
    """Bring the index's rows for the companions this run wrote/deleted up to date.

    Generated companions are excluded from the freshness watermark (#37) - they
    are written FROM the index, so counting them made every view write stale the
    index it had just read. But index.py still puts each companion's body into
    `notes_fts`, so with the watermark silent nothing else would ever notice
    that text changed: `fha find --text` would keep serving the PREVIOUS
    timeline's words, and a `views clean` would leave rows for files that are
    gone. The row rewrite lives in `_lib.sync_generated_view_rows` because views
    cannot import index (tools never import tools).

    The read handle is closed first: the sync writes to the same SQLite file,
    and a lingering reader can block that write on some platforms. Returns the
    shared status ('indexed' / 'index_absent' / 'index_error') - and every
    caller must turn 'index_error' into a WARNING exit, not just print advice.
    A run that says "clean" leaves nothing else to notice the problem: the
    watermark never counts a generated companion, so index.sqlite stays
    "fresh" over rows that no longer match the files on disk.
    """
    if conn is not None:
        conn.close()
    status = sync_generated_view_rows(archive_root, written=written, removed=removed)
    if status == 'index_error':
        print(
            'WARNING: the view files are written, but the search index could '
            'not be updated to match them. Run `fha index` so searches stop '
            'returning the old text.',
            file=sys.stderr,
        )
    return status


def _sync_written_views(
    archive_root: Path,
    conn: sqlite3.Connection | None,
    written: list[Path],
    noun: str,
) -> str:
    """Update the index rows for companions just written, and say the right thing.

    All four write paths (the three content views and `refresh`) owe the human
    two different sentences, so the choice lives here once. 'index_absent'
    means there is no index to keep in step - not this run's doing, and the
    ordinary "run `fha index` when convenient" reminder covers it. 'index_error'
    means an index IS there and now disagrees with the files; `_sync_index_rows`
    has already said so on stderr with the fix, and repeating "when convenient"
    would understate a rebuild the next search depends on. `noun` carries the
    caller's own singular/plural wording so the reminder reads naturally.
    """
    status = _sync_index_rows(archive_root, conn=conn, written=written)
    if status == 'index_absent':
        print(
            'Run `fha index` when convenient to update the search index '
            f'with the new view {noun}.'
        )
    return status


# ── HTML single-file rendering ────────────────────────────────────────────────
# `--format html` renders each content view as one standalone HTML file under
# generated/views/.  The conventions here (marker before the doctype, inline
# CSS subset, not-for-publication banner, plain-text [[ID]] tokens, exit-0
# writes) are shared by every fha single-file HTML artifact - TOOLING §7 D11
# is the authoritative statement; the module docstring carries the summary.

# Matches [[target]] and [[target|display]] citation tokens in an already
# markdown-rendered view line.  Applied AFTER html-escaping (escaping touches
# neither brackets nor the pipe), so the display text is already safe.
_TOKEN_RE = re.compile(r'\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]')


def _md_inline_to_html(text: str) -> str:
    """Render one finished .md view line as HTML.

    The HTML views are deliberately derived from the same rendered lines the
    .md serializer writes - one query, one line format, two wrappers - so the
    two formats cannot drift in content (TOOLING §7 D11).  Escapes the whole
    line, then turns `[[ID]]` / `[[ID|display]]` tokens into plain styled
    `<span class="cite">` text.  Never links: a standalone file has nothing
    durable to link to (owner decision 7-Q3 - views never link into
    generated/site/; the standalone-file property wins).
    """
    escaped = html_escape(text)

    def _token(m: re.Match) -> str:
        # A file-stem target shows as its ID, the same as the ID form.
        target, display = link_parts(m.group(1).split('#', 1)[0], m.group(2))
        if display:
            return f'<span class="cite" title="{target}">{display}</span>'
        return f'<span class="cite">{target}</span>'

    return _TOKEN_RE.sub(_token, escaped)


def _html_out_path(archive_root: Path, md_out_path: Path) -> Path:
    """Return (and create the folder for) the HTML twin of a .md view path.

    HTML views land under a visible generated/views/ folder, named by the .md
    twin's stem, so the two formats pair by name and the archive record tree
    stays plain text (the .md companions are Obsidian-linkable research
    artifacts; the HTML is a rendering).
    """
    out_dir = archive_root / 'generated' / 'views'
    out_dir.mkdir(parents=True, exist_ok=True)
    return _existing_spelling(out_dir / f'{md_out_path.stem}.html')


def _existing_spelling(target: Path) -> Path:
    """`target`, or the file already beside it whose name differs only in
    capitals - an older fha named views with a capital id prefix (`…_P-…`)
    and now writes `…_p-…` (SPEC §10). Regenerating that file in place,
    rather than writing a twin beside it on a case-sensitive disk, keeps one
    view per person and keeps the hand-made-file guard looking at the real
    file."""
    if target.exists():
        return target
    try:
        for existing in target.parent.iterdir():
            if existing.name.lower() == target.name.lower():
                return existing
    except OSError:
        pass
    return target


def _format_precheck(fmt: str, allowed: tuple[str, ...]) -> Result | None:
    """Validate a run_*'s fmt argument; None when generation can proceed.

    The engines are callable headlessly (not only through argparse choices),
    so the format contract is enforced here: an unknown format and a missing
    Jinja2 (the HTML renderer's one dependency, deliberately not imported on
    the md path so plain view runs never require it) both return a failure
    Result whose message names the fix.
    """
    if fmt not in allowed:
        allowed_text = ', '.join(repr(a) for a in allowed)
        print(
            f"ERROR: unknown view format {fmt!r} - choose one of {allowed_text}. "
            f"'md' writes the companion markdown file (the default); 'html' writes "
            f"a standalone printable page under generated/views/.",
            file=sys.stderr,
        )
        return _views_result(EXIT_FAILURE)
    if fmt != 'md':
        try:
            import jinja2  # noqa: F401  - availability probe only
        except ImportError:
            print(
                f'ERROR: --format html needs the Jinja2 library, which is not '
                f'installed. Install it with `{pip_command("jinja2")}` (or '
                f'`{pip_command("-r " + requirements_hint())}`), or run without '
                f'--format html for the markdown view.',
                file=sys.stderr,
            )
            return _views_result(EXIT_FAILURE)
    return None


# The CSS read and template load are cached per process inside _lib (the shared
# single-file HTML infra); this flag preserves views' own contract of printing
# the "css missing" warning at most ONCE per process even across a bulk
# `views refresh --format both` that renders many files.
_VIEW_CSS_WARNED = False


def _view_css() -> str:
    """Return design/view.css for inlining ('' when missing), warning once.

    Delegates the (cached) read and the warning text to the shared loader; the
    warning is printed here - the interface layer - once per process, exactly
    as before, since re-reading is what the cache prevents, not re-warning.
    A missing file degrades to an unstyled-but-complete page.
    """
    global _VIEW_CSS_WARNED
    css, warning = load_view_css('the HTML view')
    if warning and not _VIEW_CSS_WARNED:
        print(warning, file=sys.stderr)
        _VIEW_CSS_WARNED = True
    return css


def _render_view_html(archive_root: Path, subcommand: str, title: str, body_html: str,
                      banner: str | None = None) -> str:
    """Wrap a rendered body in the standalone single-file page shell.

    The GENERATED marker is the file's FIRST line, before <!DOCTYPE html> - a
    comment there is valid HTML (no quirks mode) and satisfies the exact same
    `is_generated_text` ownership test the .md views use, so `_write_view_file`
    and `views clean` need no format-specific logic.  The masthead title comes
    from fha.yaml `site: archive_name:` (the `fha site` key, with the legacy
    top-level fallback) so the printed page names the archive it came from.
    Autoescape (view.html, ON) escapes title/masthead/date; the pre-rendered
    fragments (marker, css, body) pass through `| safe` in the template - the
    body is built exclusively by the serializers below, which escape every
    piece of record text they interpolate. `banner` replaces the private-
    companion line for the one kind of page that is made to be handed out
    (the printed reports); left out, every other view keeps its banner. Callers reach this only after
    _format_precheck proved Jinja2 importable; render_template translates a
    missing/broken template file into a plain RuntimeError.
    """
    marker = _gen_header(subcommand).rstrip('\n')
    return render_template(
        'view.html',
        marker=marker,
        title=title,
        subcommand=subcommand,
        date=_today(),
        archive_title=archive_title(load_fha_yaml(archive_root)),
        css=_view_css(),
        body=body_html,
        banner=banner,
    ) + '\n'


def _profile_path_for(conn: sqlite3.Connection, person_id: str, archive_root: Path) -> Path | None:
    """Return the absolute path to the person's profile file, or None."""
    row = conn.execute(
        "SELECT path FROM person_files WHERE person_id = ? AND kind = 'profile'",
        (person_id,),
    ).fetchone()
    if not row:
        return None
    return archive_root / row['path']


def _out_path_for(profile_path: Path, kind: str, person_id: str) -> Path:
    """
    Construct the output path for a generated companion file.

    Companion files sit in the same directory as the profile and share its
    name prefix so they sort together in file explorers and travel together
    in git diffs - but "together" alone let the disposable draft-queue cache
    sort AHEAD of the person's own record, since the kind word decided the
    order and 'd' (draft-queue) sorts before 'p' (the bare P-id profile)
    (#77). A `view_` marker immediately before the kind word fixes that
    without touching the profile or the research file: `view` sorts after
    both the bare `p-{id}` profile and `research`, so the three generated
    companions land together, below the two human-facing files:

        hartley__thomas_edward_P-de957bcda1.md               ← profile
        hartley__thomas_edward_research_P-de957bcda1.md      ← research file
        hartley__thomas_edward_view_timeline_P-de957bcda1.md ← this function's output

    The transformation: strip the trailing _{P-id}, append _view_{kind}_{P-id}.
    Nothing else is stripped - and deliberately so.

    A profile stem CAN already end in a kind word, because SPEC §13's kind slot
    and the last given-name segment are the same slot: Marie Timeline Hartley's
    record is `hartley__marie_timeline_P-…`, and her timeline comes out
    `hartley__marie_timeline_view_timeline_P-…`. The doubled word looks like a
    bug and is not one. Removing it would be: the only way to tell a "prior-
    generation kind token" from a given name is to read the profile's own
    frontmatter, and the file this function is handed IS the profile (its path
    comes from the `kind = 'profile'` row), so stripping the word would aim the
    generated timeline straight at her record's own filename and overwrite a
    person's record with machine output. A harmless odd name beats that trade
    every time.

    So the doubled name is left alone here, and the ambiguity that produced it
    is reported where a human can settle it: `fha lint`'s W122 names this exact
    filename when it offers the rename that ends the confusion.

    The `view_` marker is inserted unconditionally, with no kind check, because
    this function has exactly one job: it is called only for the two remaining
    GENERATED companion kinds (`fha views timeline|draft-queue`) - sources-index
    stopped calling this function at all when its per-person form became an
    inline `## Sources` region rather than a companion file (#76). The
    research companion is written by a different path entirely (SPEC §16) that never calls this function, so `research` filenames are
    untouched by this change without needing a branch to exclude them here.
    """
    stem = profile_path.stem   # e.g. hartley__thomas_edward_P-de957bcda1
    # Strip trailing _{P-id} suffix (Crockford Base32 alphabet, case-insensitive)
    base = re.sub(r'_[PSCLH]-[0-9a-hjkmnp-tv-z]{10}$', '', stem, flags=re.I)
    # Filename convention: the id is written all lowercase (SPEC §10). `view_`
    # (#77) sorts after both the bare profile and `_research_`, so the three
    # generated companions sort together, below the two human-facing files.
    return _existing_spelling(
        profile_path.parent / f'{base}_view_{kind}_{fmt_id_display(person_id)}.md')


def _format_sid(source_id: str) -> str:
    """Format a source ID as a citation token: s-xxx -> [[S-xxx]]."""
    return f'[[{fmt_id_display(source_id)}]]'


def _place_label(place_text: str | None, place_id: str | None,
                 conn: sqlite3.Connection) -> str | None:
    """
    Return a place display string for a claim line.

    A generated view is markdown (not the bare claims YAML), so this is one of
    the few surfaces where a claim's place is written as a link: a registered
    `place_id` renders as `[[L-…|label]]`, which the fha tools and the site
    follow (Obsidian follows links by file name only, and places have no
    files, so it shows this one as unresolved) (ID load-bearing, the
    as-written text preserved as display); a claim with only free `place_text`
    and no L-id stays plain. Prefers place_text over the registry name for the label.
    """
    label = place_text
    if not label and place_id:
        row = conn.execute('SELECT name FROM places WHERE id = ?', (place_id,)).fetchone()
        if row and row['name']:
            label = row['name']
    if not label:
        return None
    if place_id:
        return f'[[{fmt_id_display(place_id)}|{label}]]'
    return label


def _curated_person_ids(conn: sqlite3.Connection) -> list[str]:
    """Return IDs of all persons with tier='curated'."""
    rows = conn.execute("SELECT id FROM persons WHERE tier = 'curated'").fetchall()
    return [r['id'] for r in rows]


_RESERVED_VIEW_FOLDERS = ('stubs',)


def _reserved_view_folder(profile_p: Path | None) -> str | None:
    """The reserved folder a profile sits directly under, else None.

    Companion views live beside a curated profile wherever it is filed - a
    couple folder or, since #80, people/connections/ (a flat, non-numbered
    home for a curated person with no derived Ahnentafel position, SPEC
    §12.3: their per-person timeline/sources-index/draft-queue work exactly
    like a couple-folder person's, since those are written beside the
    profile regardless of which folder that is). people/stubs/ is the
    only folder a companion view still refuses to write beside.
    """
    if profile_p is not None and profile_p.parent.name.lower() in _RESERVED_VIEW_FOLDERS:
        return profile_p.parent.name.lower()
    return None


def _view_eligible_curated_ids(
    conn: sqlite3.Connection, archive_root: Path
) -> list[str]:
    """Curated person ids whose profile lives in a maintainable location.

    The bulk view paths (`--all-curated`, `views refresh`) generate a companion
    file *beside each profile* (`_out_path_for`). A curated-tier record still
    parked under people/stubs/ would get a GENERATED file written into stubs/ -
    the exact case `_skip_stub_person` refuses on the per-person path - so it is
    filtered out here too (with a one-line note), keeping the tier-and-location
    rule uniform across every generation path.
    """
    eligible: list[str] = []
    for pid in _curated_person_ids(conn):
        profile_p = _profile_path_for(conn, pid, archive_root)
        folder = _reserved_view_folder(profile_p)
        if folder is not None:
            row = conn.execute('SELECT name FROM persons WHERE id = ?', (pid,)).fetchone()
            name = row['name'] if row else pid
            print(
                f"  skipped {pid} ({name}): curated record still under people/{folder}/ - "
                f"promote it into its couple folder to generate its companion views "
                f"(`fha person promote {fmt_id_display(pid)}` finishes the move)",
                file=sys.stderr,
            )
            continue
        eligible.append(pid)
    return eligible


def _empty_curated_views_result(conn: sqlite3.Connection) -> Result:
    """Result for a bulk view run that found no *eligible* curated person.

    Distinguishes 'no curated persons at all' (clean, exit 0 - nothing to do) from
    'every curated record is still parked under people/stubs/' (a warning): the
    latter generated nothing yet leaves real work to do, so it must not read as
    up-to-date to a caller or to automation.
    """
    if _curated_person_ids(conn):
        print(
            'All curated persons are still under people/stubs/ - nothing was '
            'generated; promote them first (`fha person promote <P-id>` per '
            'person, or `fha views brackets --fix-promote` for the whole direct '
            'line; a non-direct person needs `--into connections/` by hand, '
            'SPEC §12.3).',
            file=sys.stderr,
        )
        return _views_result(EXIT_WARNINGS, data={'count': 0})
    print('No curated persons found in index.')
    return _views_result(EXIT_CLEAN, data={'count': 0})


def _skip_stub_person(
    conn: sqlite3.Connection, pid: str, view_name: str, archive_root: Path
) -> bool:
    """True when pid must not get a companion view - a non-curated (stub) person,
    or any record still parked under people/stubs/ - with a plain note either way.

    Companion views (timeline, draft-queue) are curated-person files (SPEC
    §16); they are written *beside the profile* (`_out_path_for`), so a
    curated person's views only stay in a maintained location when the profile
    lives in a couple folder. Two states must be refused:
      - a non-curated (stub/connection) person - a companion file is meaningless;
      - a record whose frontmatter was flipped to `tier: curated` but that still
        physically lives in people/stubs/ - writing beside it drops a GENERATED
        file into stubs/, the wrong home for a curated person's views.
    Guarding on tier *and* location keeps the rule enforced here, not remembered by
    every caller. An unknown pid returns False - the generator's own "no profile
    found" warning covers that case.

    NOT called for per-person sources-index (#76): that form rewrites a
    section INSIDE the profile wherever it already lives, so neither of the
    two states above applies to it - a stub has just as legitimate a `##
    Sources` section as a curated person (see `run_sources_index`'s
    docstring), and there is no "wrong folder for a GENERATED file" risk
    when nothing new is being created beside anything.
    """
    row = conn.execute('SELECT tier, name FROM persons WHERE id = ?', (pid,)).fetchone()
    if row is None:
        return False
    profile_p = _profile_path_for(conn, pid, archive_root)
    reserved = _reserved_view_folder(profile_p)
    is_curated = (row['tier'] or '').lower() == 'curated'
    if is_curated and reserved is None:
        return False
    reason = (
        f'is curated but still lives under people/{reserved}/' if is_curated
        else f"is a {row['tier']} person"
    )
    print(
        f"{pid} ({row['name']}) {reason} - companion views like the {view_name} "
        f"belong to a curated person filed in a couple folder or people/"
        f"connections/ (SPEC §16), so nothing was written. To generate one, "
        f"promote the record first - `fha person promote {fmt_id_display(pid)}` "
        f"does it in one step for a direct-line person, or add `--into "
        f"connections/` for anyone else (SPEC §12.3); otherwise set `tier: "
        f"curated` and move it out of people/stubs/ by hand - then re-run.",
        file=sys.stderr,
    )
    return True


# ── Timeline ──────────────────────────────────────────────────────────────────

def _decade_from_edtf(date_edtf: str | None) -> str | None:
    """
    Return a '### 1880s' header string for the decade of an EDTF date, or None.

    Extracts the year from date_edtf directly (e.g. '1840~' → 1840 → '### 1840s')
    rather than from date_min, which is widened for approximate dates and would
    give the wrong decade (e.g. '1840~' has date_min '1839-01-01').
    """
    if not date_edtf:
        return None
    # Handle interval A/B - use the start
    edtf = date_edtf.split('/')[0].strip()
    # Strip qualifiers: ~, ?, [..
    edtf = edtf.lstrip('[.').rstrip('~?]')
    # EDTF decade form, e.g. '185X' - the century+decade digits are explicit
    # and the units digit is a literal 'X', so int() on the full year fails.
    if len(edtf) >= 4 and edtf[:3].isdigit() and edtf[3] in ('X', 'x'):
        decade = int(edtf[:3]) * 10
        return f'### {decade}s'
    try:
        year = int(edtf[:4])
        decade = (year // 10) * 10
        return f'### {decade}s'
    except (ValueError, IndexError):
        return None


def _timeline_claim_line(row: sqlite3.Row, conn: sqlite3.Connection) -> str:
    """Format one timeline line from a claims query row.

    A claim that is not settled fact carries a plain-word tag at the end of
    its line: '[unconfirmed - parked {date}]' for a suggested claim that
    carries a reviewed: date (looked at, not settled yet - SPEC §8.1),
    '[low confidence]' for an accepted claim whose evidence quality is low
    (SPEC §8.5 keeps status and confidence orthogonal: an accepted
    low-confidence fact is built on, and flagged as worth corroborating when
    a better record surfaces). Never-reviewed suggested rows get no tag -
    they already render under their own 'Unreviewed' header. Requires the
    query row to carry status/confidence/reviewed alongside the display
    columns.
    """
    date_str = row['date_edtf'] or '(undated)'
    place = _place_label(row['place_text'], row['place_id'], conn)
    line = f'{date_str} - {row["type"]}: {row["value"]}'
    if place:
        line += f' @ {place}'
    line += f' {_format_sid(row["source_id"])}'
    if row['status'] == 'suggested' and row['reviewed']:
        line += f' [unconfirmed - parked {row["reviewed"]}]'
    elif row['status'] == 'accepted' and row['confidence'] == 'low':
        line += ' [low confidence]'
    return line


def _timeline_body_html(
    conn: sqlite3.Connection,
    sections: list[tuple[str | None, list[sqlite3.Row]]],
    suggested_rows: list[sqlite3.Row],
) -> str:
    """Render the timeline sections as an HTML body fragment.

    Consumes the exact section structure the .md serializer renders and reuses
    `_timeline_claim_line` for every entry (the finished .md line, escaped and
    token-styled by `_md_inline_to_html`), so the HTML twin can never say
    something the .md does not.
    """
    parts: list[str] = []
    for decade_hdr, rows in sections:
        if not rows:
            continue
        if decade_hdr:
            # decade_hdr is the .md '### 1880s' header; strip the '### ' prefix.
            parts.append(f'<h3>{html_escape(decade_hdr[4:])}</h3>')
        items = ''.join(
            f'<li>{_md_inline_to_html(_timeline_claim_line(r, conn))}</li>'
            for r in rows
        )
        parts.append(f'<ul class="timeline">{items}</ul>')
    if suggested_rows:
        parts.append('<h2>Unreviewed</h2>')
        items = ''.join(
            f'<li>{_md_inline_to_html(_timeline_claim_line(r, conn))}</li>'
            for r in suggested_rows
        )
        parts.append(f'<ul class="timeline">{items}</ul>')
    if not parts:
        parts.append('<p class="empty">(No claims recorded yet.)</p>')
    return '\n'.join(parts) + '\n'


def _generate_timeline(
    conn: sqlite3.Connection, person_id: str, archive_root: Path, fmt: str = 'md'
) -> Path | None:
    """
    Generate the timeline view for one person and write it to disk.

    Output structure (TOOLING §7):
      - One ### Decade section per decade of accepted and parked claims
        (parked = suggested with a reviewed: date, tagged on its line)
      - Undated claims appear at the bottom of the dated section (ORDER BY puts
        NULL date_min last)
      - ## Unreviewed section at the end for never-reviewed suggested claims
        (AI-drafted, not yet looked at by a human reviewer)

    `fmt='html'` writes the standalone single-file twin under generated/views/
    instead - same queries, same sections, same rendered lines (D11); callers
    reach the html branch only through run_timeline's _format_precheck.

    Returns the path written, or None if the person has no profile in the index.
    """
    profile_p = _profile_path_for(conn, person_id, archive_root)
    if not profile_p:
        print(f'WARNING: no profile found for {person_id} - skipped.', file=sys.stderr)
        return None

    name_row = conn.execute('SELECT name FROM persons WHERE id = ?', (person_id,)).fetchone()
    person_name = name_row['name'] if name_row else person_id

    # Sorted by each date's known bound (`claim_date_order_sql`), so "by 1895"
    # ([..1895]) files in with the dated entries instead of leading the
    # timeline under a stray decade heading. date_edtf is read separately for
    # display and decade grouping (see _decade_from_edtf).
    main_rows = conn.execute(
        f"""
        SELECT DISTINCT c.date_edtf, c.date_min, c.date_max, c.type, c.value,
               c.place_id, c.place_text, c.source_id,
               c.status, c.confidence, c.reviewed
        FROM claim_persons cp
        JOIN claims c ON cp.claim_id = c.id
        WHERE cp.person_id = ? AND (c.status = 'accepted' OR {PARKED_CLAIM_SQL})
        ORDER BY {claim_date_order_sql('c')}
        """,
        (person_id,),
    ).fetchall()

    suggested_rows = conn.execute(
        f"""
        SELECT DISTINCT c.date_edtf, c.date_min, c.date_max, c.type, c.value,
               c.place_id, c.place_text, c.source_id,
               c.status, c.confidence, c.reviewed
        FROM claim_persons cp
        JOIN claims c ON cp.claim_id = c.id
        WHERE cp.person_id = ? AND c.status = 'suggested' AND NOT {PARKED_CLAIM_SQL}
        ORDER BY {claim_date_order_sql('c')}
        """,
        (person_id,),
    ).fetchall()

    out_path = _out_path_for(profile_p, 'timeline', person_id)

    # Accumulate rows into (decade_header, rows) sections before rendering.
    # We cannot emit section headers inline as we iterate because Markdown needs
    # a blank line BEFORE each ### header - we don't know whether a new section
    # is starting until we've already seen the first row of it.  Collecting first
    # lets us emit clean separators in a second pass without lookahead.
    sections: list[tuple[str | None, list[sqlite3.Row]]] = []
    current_decade: str | None = 'UNSET'   # sentinel; distinct from None (= undated)
    current_rows: list[sqlite3.Row] = []
    for row in main_rows:
        decade_hdr = _decade_from_edtf(row['date_edtf'])
        if decade_hdr != current_decade:
            if current_rows or current_decade != 'UNSET':
                sections.append((current_decade if current_decade != 'UNSET' else None, current_rows))
            current_decade = decade_hdr
            current_rows = []
        current_rows.append(row)
    if current_rows or current_decade != 'UNSET':
        sections.append((current_decade if current_decade != 'UNSET' else None, current_rows))

    if fmt == 'html':
        body = _purpose_block_html('timeline') + _timeline_body_html(conn, sections, suggested_rows)
        return _write_view_file(
            _html_out_path(archive_root, out_path),
            _render_view_html(
                archive_root, 'timeline', f'Timeline: {person_name}', body),
        )

    parts: list[str] = [
        _gen_header('timeline'), f'# Timeline: {person_name}\n', _purpose_block_md('timeline'),
    ]

    for decade_hdr, rows in sections:
        if not rows:
            continue
        if decade_hdr:
            parts.append(f'\n{decade_hdr}\n\n')
        else:
            parts.append('\n')
        parts.append('\n'.join(f'- {_timeline_claim_line(r, conn)}' for r in rows))
        parts.append('\n')

    if suggested_rows:
        parts.append('\n## Unreviewed\n\n')
        parts.append('\n'.join(f'- {_timeline_claim_line(r, conn)}' for r in suggested_rows))
        parts.append('\n')

    return _write_view_file(out_path, ''.join(parts), _view_links(archive_root))


# ── Sources-index ─────────────────────────────────────────────────────────────

def _source_ids_for_persons(conn: sqlite3.Connection, person_ids: list[str]) -> list[str]:
    """
    Return distinct source IDs linked to any of the given persons.

    WHY UNION: the index tracks person-source links in two separate tables.
      - claim_persons → claims: covers sources that produced specific claims
        (e.g. a census record that generated a birth-year claim)
      - source_people: a direct person-source table for sources that are "about"
        a person even without specific extracted claims (e.g. a family bible
        that names someone but hasn't been processed into individual claims yet)
    The UNION catches both so the sources-index is complete.
    """
    if not person_ids:
        return []
    placeholders = ','.join('?' * len(person_ids))
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
        person_ids + person_ids,
    ).fetchall()
    return [r[0] for r in rows]


def _source_rows_by_type(
    conn: sqlite3.Connection, source_ids: list[str],
) -> dict[str, list[sqlite3.Row]]:
    """Source rows for `source_ids`, grouped by `source_type` (falling back to
    'other') and sorted alphabetically within each group - the one query both
    the per-person region renders from."""
    if not source_ids:
        return {}
    placeholders = ','.join('?' * len(source_ids))
    rows = conn.execute(
        f"""
        SELECT id, title, source_type, path
        FROM sources
        WHERE id IN ({placeholders})
        ORDER BY source_type, title
        """,
        source_ids,
    ).fetchall()
    by_type: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        st = row['source_type'] or 'other'
        by_type.setdefault(st, []).append(row)
    return by_type


def _sources_index_region_body(by_type: dict[str, list[sqlite3.Row]]) -> str:
    """Render grouped source rows for the per-person `## Sources` REGION -
    nested under an existing `## Sources` heading, so groups are bold labels
    rather than a second level of `## ` headings (which would misparse as a
    sibling profile section - `section_bounds` reads `## ` as a boundary)."""
    if not by_type:
        return '*(No sources found.)*'
    # A blank line before each group's label: directly under the previous
    # group's last bullet, Markdown would read the label as more of that bullet.
    groups: list[str] = []
    for source_type in sorted(by_type.keys()):
        lines = [f'**{source_type}**', '']
        for row in by_type[source_type]:
            sid_token = _format_sid(row['id'])
            path_text = row['path'].replace('\\', '/')
            lines.append(f'- {row["title"]} {sid_token} - {path_text}')
        groups.append('\n'.join(lines))
    return '\n\n'.join(groups)


def _generate_sources_index_person(
    conn: sqlite3.Connection, person_id: str, archive_root: Path, fmt: str = 'md',
    written: set[Path] | None = None,
) -> Path | None:
    """Refresh the person's OWN `## Sources` GENERATED-BEGIN/END region in
    place; return the profile path written, or None.

    #76 retired the separate per-person companion file entirely - what used
    to be `_out_path_for(profile_p, 'sources-index', person_id)` is now a
    surgical rewrite of one section INSIDE the profile (`_lib.
    write_generated_region`), so there is no output path to construct and no
    whole-file GENERATED-header write (`_write_view_file`) to reach for. This
    is also the one content view that works on a STUB: SPEC §16's whole point
    for a stub carrying this section is that its sources are already listed
    by the time someone promotes it, not regenerated from scratch afterward -
    so, unlike timeline/draft-queue, this function does NOT gate on curated
    tier (callers decide whether to call it for a stub; the single-person CLI
    path does, the `--all-curated` batch path does not, by that flag's own
    name - see `run_sources_index`).

    `fmt='html'` is refused by the caller before this is ever reached (no
    separate file means no HTML twin to build) - this function only ever
    writes `.md` (the profile IS markdown either way).

    `written`, when given, collects the profile path only when the file was
    actually rewritten, so a batch re-syncs the index for changed profiles
    alone instead of every one it looked at.
    """
    profile_p = _profile_path_for(conn, person_id, archive_root)
    if not profile_p:
        print(f'WARNING: no profile found for {person_id} - skipped.', file=sys.stderr)
        return None

    source_ids = _source_ids_for_persons(conn, [person_id])
    by_type = _source_rows_by_type(conn, source_ids)
    body = _sources_index_region_body(by_type)
    # The region's citations in the archive's link form (SPEC §18).
    body = _view_links(archive_root).convert(body)

    try:
        text = read_text_exact(profile_p)
    except OSError as e:
        print(f'WARNING: could not read {profile_p} ({e.strerror or e}) - skipped.',
              file=sys.stderr)
        return None
    new_text, _created = write_generated_region(
        text, heading='Sources', region_name='sources-index',
        subcommand='sources-index', body=body)
    if new_text == text:
        return profile_p   # already current - nothing to write, still a success
    write_text_exact_atomic(profile_p, reapply_newline(new_text, text))
    if written is not None:
        written.add(profile_p)
    return profile_p


# ── Summary region ────────────────────────────────────────────────────────────

def _summary_person_link(conn: sqlite3.Connection, pid: str) -> str:
    """`[[p-…|Name]]` for a person in the index, `[[p-…]]` when nameless."""
    row = conn.execute('SELECT name FROM persons WHERE id = ?', (pid,)).fetchone()
    entry = link_entry(pid, row['name'] if row else None)
    return entry if entry.startswith('[[') else f'[[{entry}]]'


def _summary_when_where(row: sqlite3.Row, conn: sqlite3.Connection, sep: str = ' - ') -> str:
    """'3 March 1840 - Easton, New York' from one claim row (either half may
    be missing; `sep` joins them). The place is shown as written, never as a
    link."""
    date = humanize_edtf(row['date_edtf']) if row['date_edtf'] else ''
    place = row['place_text'] or ''
    if not place and row['place_id']:
        p = conn.execute('SELECT name FROM places WHERE id = ?', (row['place_id'],)).fetchone()
        place = (p['name'] if p else '') or ''
    return sep.join(x for x in (date, place) if x)


def _summary_own_vital(
    conn: sqlite3.Connection, pid: str, types: tuple[str, ...], cache: dict,
) -> sqlite3.Row | None:
    """The person's own most exact accepted, non-negated vital of the first
    type in `types` that has one (`vital_pick_key`, `claim_is_own_vital`) -
    the same pick the site's summary and the tree labels make."""
    for ctype in types:
        rows = conn.execute(
            'SELECT DISTINCT c.id, c.type, c.date_edtf, c.date_min, c.date_max, '
            'c.place_id, c.place_text, c.source_id, c.confidence '
            'FROM claims c JOIN claim_persons cp ON cp.claim_id = c.id '
            "WHERE cp.person_id = ? AND c.status = 'accepted' "
            'AND COALESCE(c.negated, 0) = 0 AND c.type = ?',
            (pid, ctype)).fetchall()
        rows = [r for r in rows if claim_is_own_vital(conn, pid, r['id'], ctype, cache)]
        if rows:
            return sorted(rows, key=vital_pick_key)[0]
    return None


def _summary_kin(conn: sqlite3.Connection, pid: str, rel: str) -> list[tuple[str, list[sqlite3.Row]]]:
    """[(other p-id, [backing claim rows])] for one kind of derived edge,
    each person once, in the order a reader expects: parents father first,
    children and spouses by date."""
    rows = conn.execute(
        'SELECT r.other_id, c.id, c.type, c.date_edtf, c.date_min, c.date_max, '
        'c.place_id, c.place_text, c.source_id, c.confidence, '
        'p.name AS other_name, p.sex AS other_sex '
        'FROM relationships r JOIN claims c ON c.id = r.claim_id '
        'LEFT JOIN persons p ON p.id = r.other_id '
        'WHERE r.person_id = ? AND r.rel = ? ORDER BY c.id',
        (pid, rel)).fetchall()
    by_other: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        if r['other_id'] != pid:
            by_other.setdefault(r['other_id'], []).append(r)

    def born(other: str) -> str:
        row = conn.execute(
            'SELECT MIN(c.date_min) AS d FROM claims c '
            'JOIN claim_persons cp ON cp.claim_id = c.id '
            "WHERE cp.person_id = ? AND c.type = 'birth' AND c.status = 'accepted' "
            'AND COALESCE(c.negated, 0) = 0', (other,)).fetchone()
        return (row['d'] if row else None) or '9999'

    def order(item: tuple[str, list[sqlite3.Row]]):
        other, claims = item
        name = (claims[0]['other_name'] or '').lower()
        if rel == 'parent':
            sex = (claims[0]['other_sex'] or '').upper()
            return ({'M': 0, 'F': 1}.get(sex, 2), name, other)
        if rel == 'spouse':
            dates = [c['date_min'] for c in claims if c['type'] == 'marriage' and c['date_min']]
            return (min(dates) if dates else '9999', name, other)
        return (born(other), name, other)

    return sorted(by_other.items(), key=order)


def _summary_couple_claims(
    conn: sqlite3.Connection, pid: str, other: str, ctype: str, cache: dict,
) -> list[sqlite3.Row]:
    """Accepted, non-negated `ctype` claims (a couple claim: `divorce`) that
    are about THESE TWO as the couple - not merely naming both. A divorce
    record that names the couple's parents in passing is not the parents'
    divorce: who the couple is comes from the claim's roles, by the one rule
    every tool uses (`claim_is_own_vital` / `vital_subjects`)."""
    rows = conn.execute(
        'SELECT DISTINCT c.id, c.type, c.date_edtf, c.date_min, c.date_max, '
        'c.place_id, c.place_text, c.source_id, c.confidence FROM claims c '
        'JOIN claim_persons a ON a.claim_id = c.id AND a.person_id = ? '
        'JOIN claim_persons b ON b.claim_id = c.id AND b.person_id = ? '
        "WHERE c.type = ? AND c.status = 'accepted' AND COALESCE(c.negated, 0) = 0",
        (pid, other, ctype)).fetchall()
    return [r for r in rows
            if claim_is_own_vital(conn, pid, r['id'], ctype, cache)
            and claim_is_own_vital(conn, other, r['id'], ctype, cache)]


def _summary_events(claims, ctype: str, breaks: list[str] = ()) -> list:
    """One claim per distinct event of `ctype`, in date order, the most
    exact claim (`vital_pick_key`) standing for each.

    Records of one couple's marriage are ONE wedding unless a divorce falls
    between them (`breaks`, the divorce dates): a record saying "about 1923"
    beside a certificate's 5 July 1921 is the same wedding remembered
    loosely, not a second one. An undated claim counts only when no claim is
    dated - it cannot say which wedding it means."""
    of_type = [c for c in claims if c['type'] == ctype]
    dated = [c for c in of_type if c['date_min']]
    if not dated:
        return sorted(of_type, key=vital_pick_key)[:1]
    groups: dict[int, list] = {}
    for c in dated:
        groups.setdefault(sum(1 for b in breaks if b and b <= c['date_min']), []).append(c)
    return [sorted(groups[k], key=vital_pick_key)[0] for k in sorted(groups)]


def _summary_cite(source_ids: list[str]) -> str:
    """One citation per fact (SPEC §16: one per line is enough in a summary)."""
    return _format_sid(source_ids[0]) if source_ids else ''


def _summary_region_body(conn: sqlite3.Connection, pid: str) -> str:
    """The Born / Died / Married / Parents / Children lines for one person,
    from accepted claims only (SPEC §16). A line with nothing to say is left
    out; every fact carries the source it came from."""
    cache: dict = {}
    out: list[str] = []
    for label, types, verb in (('Born', ('birth', 'baptism'), 'baptized'),
                               ('Died', ('death', 'burial'), 'buried')):
        row = _summary_own_vital(conn, pid, types, cache)
        if row is None:
            continue
        text = _summary_when_where(row, conn)
        if row['type'] != types[0]:
            text = f'{verb} {text}'.strip()
        out.append(' '.join(x for x in (f'**{label}:**', text,
                                        _summary_cite([row['source_id']])) if x))

    # One entry per WEDDING, not per spouse, in date order: a couple who
    # married, divorced and married again shows both weddings and the divorce
    # between them (SPEC §16). Records of one couple's marriage are one entry
    # unless a divorce falls between them (`_summary_events`), headed by the
    # most exact (`vital_pick_key`).
    entries: list[tuple[str, str]] = []
    for other, claims in _summary_kin(conn, pid, 'spouse'):
        link = _summary_person_link(conn, other)
        divorces = sorted(
            (d for d in _summary_couple_claims(conn, pid, other, 'divorce', cache)
             if d['date_min']),
            key=lambda d: d['date_min'])
        weddings = _summary_events(claims, 'marriage', [d['date_min'] for d in divorces])
        if not weddings:
            # Known as a couple only from a relationship record: still say
            # so, and say they divorced if a record says that.
            text = ' '.join(p for p in (link, _summary_cite([claims[0]['source_id']])) if p)
            if divorces:
                d = divorces[0]
                text += '; ' + ' '.join(p for p in (
                    'divorced', _summary_when_where(d, conn, ', '),
                    _summary_cite([d['source_id']])) if p)
            entries.append(('9999', text))
            continue
        for n, w in enumerate(weddings):
            when = _summary_when_where(w, conn, ', ')
            text = ' '.join(p for p in (link, f'- {when}' if when else '',
                                        _summary_cite([w['source_id']])) if p)
            nxt = weddings[n + 1]['date_min'] if n + 1 < len(weddings) else None
            ended = [d for d in divorces
                     if (d['date_min'] or '') >= (w['date_min'] or '')
                     and (nxt is None or (d['date_min'] or '') < nxt)]
            if ended:
                d = ended[0]
                when_d = _summary_when_where(d, conn, ', ')
                text += '; ' + ' '.join(p for p in (
                    'divorced', when_d, _summary_cite([d['source_id']])) if p)
            entries.append((w['date_min'] or '9999', text))
    married = [text for _key, text in sorted(entries, key=lambda e: e[0])]
    if not married:
        # A marriage record naming only this person still says they married.
        row = _summary_own_vital(conn, pid, ('marriage',), cache)
        if row is not None:
            married.append(' '.join(x for x in (_summary_when_where(row, conn, ', '),
                                                _summary_cite([row['source_id']])) if x))
    if married:
        out.append('**Married:** ' + ' · '.join(married))

    for label, rel in (('Parents', 'parent'), ('Children', 'child')):
        people = [f'{_summary_person_link(conn, other)} {_summary_cite([c["source_id"] for c in claims])}'
                  for other, claims in _summary_kin(conn, pid, rel)]
        if people:
            out.append(f'**{label}:** ' + ' · '.join(people))
    return '\n'.join(out)


def _summary_completeness_context(conn: sqlite3.Connection, archive_root: Path) -> dict | None:
    """The completeness engine's context when fha.yaml asks for the
    `**Records:**` line (`views: completeness_line: true`), else None.

    Off unless the owner turns it on, so no archive's profiles change until
    they ask. Read once per run: a whole-archive refresh asks it of every
    person, and the context is the whole index in memory."""
    cfg = load_fha_yaml(archive_root)
    block = cfg.get('views') if isinstance(cfg, dict) else None
    value = block.get('completeness_line') if isinstance(block, dict) else None
    if value is None or value is False:
        return None
    if value is not True:
        print(f'WARNING: fha.yaml views: completeness_line: is {value!r}; write true or '
              'false. Leaving the Records line out for now.', file=sys.stderr)
        return None
    return completeness_context(conn)


def _summary_records_line(ctx: dict | None, pid: str) -> str:
    """'**Records:** 3 of 5 - no marriage record; censuses 2 of 6 years', or
    '' when the line is off or the person is not in the engine's context
    (a merged record). Read from accepted claims, like every other summary
    line, through the shared engine `fha metrics` uses (TOOLING §7a), so
    the two can never disagree."""
    if ctx is None or normalize_id(pid) not in ctx['persons']:
        return ''
    result = person_completeness(ctx, pid, mode='accepted')
    return f"**Records:** {completeness_summary_line(result, sep=' - ')}"


def _generate_summary_person(
    conn: sqlite3.Connection, person_id: str, archive_root: Path, fmt: str = 'md',
    completeness: dict | None = None, written: set[Path] | None = None,
) -> Path | None:
    """Write the person's summary region (`_lib.write_summary_region`);
    return the profile path when it is current afterwards, else None.
    `written`, when given, collects the path only when the file was rewritten
    (see `_generate_sources_index_person`).

    Like `## Sources`, the region lives inside the profile, so there is no
    companion file and no HTML twin. A profile whose summary lines a human
    wrote by hand (no markers) is theirs: it is reported and left alone, and
    counts as current - there is nothing for this command to do there.
    `completeness` is the engine context from `_summary_completeness_context`
    (None: no Records line)."""
    profile_p = _profile_path_for(conn, person_id, archive_root)
    if not profile_p:
        print(f'WARNING: no profile found for {person_id} - skipped.', file=sys.stderr)
        return None
    try:
        text = read_text_exact(profile_p)
    except (OSError, UnicodeDecodeError) as e:
        print(f'WARNING: could not read {profile_p.name} '
              f'({"not saved as UTF-8" if isinstance(e, UnicodeDecodeError) else (e.strerror or e)})'
              ' - skipped.', file=sys.stderr)
        return None
    new_text, status = write_summary_region(
        text, _view_links(archive_root).convert(_summary_region_body(conn, person_id)),
        extra=_summary_records_line(completeness, person_id))
    if status == 'damaged':
        print(f'WARNING: {profile_p.name} has a "GENERATED-BEGIN summary" marker with no '
              '"GENERATED-END summary" after it - put the end marker back (or delete '
              'both) and run this again; skipped.', file=sys.stderr)
        return None
    if status == 'hand-written':
        print(f'  {profile_p.name}: its summary lines were written by hand '
              '(no GENERATED markers) - left as they are.')
    if status == 'written':
        write_text_exact_atomic(profile_p, reapply_newline(new_text, text))
        if written is not None:
            written.add(profile_p)
    return profile_p


# ── Draft-queue ───────────────────────────────────────────────────────────────

def _body_excluding_sources_region(body: str) -> str:
    """The profile body with its `## Sources` GENERATED region cut out - the
    guard `_generate_draft_queue` runs before scanning for citation tokens.

    Every source touching a person is ALWAYS listed in that region once it
    has been refreshed once (#76) - so scanning the WHOLE body for `[[S-…]]`
    tokens (draft-queue's existing, unchanged rule: "if an S-id token appears
    ANYWHERE in the body, that source is considered cited") would read every
    one of those listings as a citation and permanently empty the queue the
    moment a profile's Sources section is first generated, even though not
    one of them was actually woven into the biography. Only tokens the human
    wrote into their OWN prose - a hand-written summary block, Biography,
    Stories, Friends & Family - should count; this is what makes that true
    again. The generated summary region goes for the same reason: it cites
    the source of every birth, death and marriage the tools found, which is
    not the human having written them up.
    """
    body = strip_generated_region(body, SUMMARY_REGION)
    lines = body.split('\n')
    bounds = section_bounds(lines, 0, 'Sources')
    if bounds is None:
        return body
    _, content_start, content_end = bounds
    return '\n'.join(lines[:content_start] + lines[content_end:])


def _generate_draft_queue(
    conn: sqlite3.Connection, person_id: str, archive_root: Path, fmt: str = 'md'
) -> Path | None:
    """
    Generate the draft-queue view for one person and write it to disk.
    `fmt='html'` writes the standalone twin under generated/views/ from the
    same diff (D11); the html branch is reached only via run_draft_queue's
    _format_precheck.

    Purpose: the draft-queue is the biographer's to-do list.  It answers the
    question "which accepted sources have I not yet cited in the profile prose?"
    A source appears in the queue when the index has accepted claims from it
    but the profile body contains no [S-id] citation token for it.

    HOW "CITED" IS DETERMINED: we scan the profile body (not the YAML front-
    matter) for citation tokens via cited_source_ids (new `[[S-…]]` or legacy
    `[S-…]`, and a claim link read as the source holding the claim).  The body
    is where the summary block and biography prose live.  If a token citing a
    source appears anywhere in the body, that source is considered cited and
    omitted from the queue.

    HOW THE DIFF WORKS:
      accepted_sids  = {source_ids with ≥1 accepted claim for this person}
      cited_sids     = {S-ids found as [S-xxx] tokens in the profile body}
      uncited_sids   = accepted_sids - cited_sids   ← the queue

    The queue lists each uncited source with its accepted claims so the
    biographer can see what facts still need to be woven into the prose.
    Returns the path written, or None if the person has no profile in the
    index, or if the profile file exists but will not decode as UTF-8 (#68).
    """
    profile_p = _profile_path_for(conn, person_id, archive_root)
    if not profile_p:
        print(f'WARNING: no profile found for {person_id} - skipped.', file=sys.stderr)
        return None

    name_row = conn.execute('SELECT name FROM persons WHERE id = ?', (person_id,)).fetchone()
    person_name = name_row['name'] if name_row else person_id

    # Same skip-and-warn idiom as the missing-profile case above, extended to
    # a profile that exists but will not decode as UTF-8 (#68): without
    # `on_decode_error`, `read_record` raises and takes the whole batch run
    # down over one person's file saved in another encoding (cp1252, a
    # Windows editor's default). The cause named here is deliberately
    # different from "no profile found" - the fix is re-saving the file's
    # encoding, not filing a profile that is already there.
    rec = read_record(profile_p, on_decode_error=lambda p: None)
    if rec['undecodable']:
        print(f'WARNING: {profile_p.name} is not saved as UTF-8 text - skipped. '
              'Open it and save it again choosing UTF-8 (in Notepad: Save As, '
              'then pick UTF-8 from the Encoding menu), then run '
              '`fha views draft-queue` again.', file=sys.stderr)
        return None
    body = rec['body']
    # Citation tokens (new `[[S-…]]` or legacy `[S-…]`), read as the sources
    # they cite: a claim link counts as its source.
    # Scanned with the ## Sources GENERATED region cut out first (#76) - see
    # _body_excluding_sources_region's own docstring for why: that region
    # always lists every source touching this person, so leaving it in would
    # read every listing as a citation and empty the queue on sight.
    cited_sids: set[str] = set(cited_source_ids(
        _body_excluding_sources_region(body), claim_source_lookup(conn)))

    accepted_rows = conn.execute(
        """
        SELECT DISTINCT c.source_id, c.type, c.value, c.date_edtf
        FROM claim_persons cp
        JOIN claims c ON cp.claim_id = c.id
        WHERE cp.person_id = ? AND c.status = 'accepted'
        ORDER BY c.source_id, c.date_min
        """,
        (person_id,),
    ).fetchall()

    accepted_sids: set[str] = {normalize_id(r['source_id']) for r in accepted_rows}
    uncited_sids = accepted_sids - cited_sids

    out_path = _out_path_for(profile_p, 'draft-queue', person_id)

    lines: list[str] = [
        _gen_header('draft-queue'), f'# Draft Queue: {person_name}\n',
        _purpose_block_md('draft-queue'),
    ]

    if not uncited_sids:
        if fmt == 'html':
            return _write_view_file(
                _html_out_path(archive_root, out_path),
                _render_view_html(
                    archive_root, 'draft-queue', f'Draft Queue: {person_name}',
                    _purpose_block_html('draft-queue')
                    + '<p>All accepted claims are cited in the profile.</p>\n'),
            )
        lines.append('\nAll accepted claims are cited in the profile.\n')
        return _write_view_file(out_path, ''.join(lines), _view_links(archive_root))

    # Fetch source metadata for uncited sources
    placeholders = ','.join('?' * len(uncited_sids))
    source_rows = conn.execute(
        f"""
        SELECT id, title, source_type
        FROM sources
        WHERE id IN ({placeholders})
        ORDER BY source_type, title
        """,
        list(uncited_sids),
    ).fetchall()

    # Index claims by source_id for quick lookup
    claims_by_source: dict[str, list[sqlite3.Row]] = {}
    for row in accepted_rows:
        sid = normalize_id(row['source_id'])
        if sid in uncited_sids:
            claims_by_source.setdefault(sid, []).append(row)

    if fmt == 'html':
        parts: list[str] = [
            _purpose_block_html('draft-queue'),
            f'<p>{len(uncited_sids)} source(s) with accepted claims '
            f'not yet cited in the profile:</p>',
        ]
        for src in source_rows:
            sid = normalize_id(src['id'])
            sid_html = _md_inline_to_html(_format_sid(src['id']))
            parts.append(
                f'<p class="queue-source"><strong>{html_escape(src["title"])}</strong> '
                f'{sid_html}</p>'
            )
            items: list[str] = []
            for claim_row in claims_by_source.get(sid, []):
                date_edtf = claim_row['date_edtf']
                date_str = f'({date_edtf})' if date_edtf else '(undated)'
                claim_line = f'{claim_row["type"]}: {claim_row["value"]} {date_str}'
                items.append(f'<li>{html_escape(claim_line)}</li>')
            if items:
                parts.append('<ul>' + ''.join(items) + '</ul>')
        return _write_view_file(
            _html_out_path(archive_root, out_path),
            _render_view_html(
                archive_root, 'draft-queue', f'Draft Queue: {person_name}',
                '\n'.join(parts) + '\n'),
        )

    lines.append(
        f'\n{len(uncited_sids)} source(s) with accepted claims not yet cited in the profile:\n'
    )

    for src in source_rows:
        sid = normalize_id(src['id'])
        sid_token = _format_sid(src['id'])
        lines.append(f'\n**{src["title"]}** {sid_token}\n')
        for claim_row in claims_by_source.get(sid, []):
            date_edtf = claim_row['date_edtf']
            date_str = f'({date_edtf})' if date_edtf else '(undated)'
            lines.append(f'  - {claim_row["type"]}: {claim_row["value"]} {date_str}\n')

    return _write_view_file(out_path, ''.join(lines), _view_links(archive_root))


# ── Brackets ─────────────────────────────────────────────────────────────────

def _parse_bracket_names(folder_name: str) -> list[str]:
    """Return names inside the [...] suffix of a folder name, or [] if absent.

    E.g. '040 Thomas Hartley + Margaret Cole [Ethel + Frances]'
    → ['Ethel', 'Frances'].  Each token is stripped of whitespace.
    """
    m = re.search(r'\[([^\]]*)\]', folder_name)
    if not m:
        return []
    return [n.strip() for n in m.group(1).split('+') if n.strip()]


def _folder_numeric_prefix(folder_name: str) -> int | None:
    """Extract the leading integer from a folder name, or None if absent."""
    m = re.match(r'^(\d+)', folder_name)
    return int(m.group(1)) if m else None


def _couple_folder_dirs(archive_root: Path) -> list[Path]:
    """Return digit-prefixed directories directly under people/, excluding
    stubs/connections. Delegates to `_lib.couple_folder_dirs` - the promote
    engine walks the same list, so the two can never disagree about what
    counts as a couple folder."""
    return couple_folder_dirs(archive_root)


def _persons_in_folder(conn: sqlite3.Connection, folder: Path) -> list[str]:
    """Return person_ids whose profile files live in `folder`.

    Matches on the second path segment in the stored alias path
    (e.g. 'people/040 Thomas…/file.md' → folder name '040 Thomas…').
    Both forward-slash and backslash variants are checked for portability.
    """
    name = folder.name
    rows = conn.execute(
        """
        SELECT person_id FROM person_files
        WHERE kind = 'profile'
          AND (path LIKE ? OR path LIKE ?)
        """,
        (f'people/{name}/%', f'people\\{name}\\%'),
    ).fetchall()
    return [r['person_id'] for r in rows]


def _build_ahnentafel_map(
    conn: sqlite3.Connection, root_pid: str,
    sex_gaps: list[dict] | None = None,
    root_position: int = 1,
) -> dict[str, int]:
    """BFS from root_pid to build {person_id → Ahnentafel position}.

    Delegates to `_lib.build_ahnentafel_map` - the derivation moved there so
    `fha person promote` and `fha report` §7b can compute the same positions
    without importing views (tools never import tools). Behavior is identical;
    the docstring and algorithm live with the shared function. `sex_gaps`
    collects the W120 set (single-parent placements defaulted for lack of a
    recorded `sex:`) when a list is passed. `root_position` is where root_pid
    itself lands - 1 for `root_generation: self` (the default), 2 for
    `root_generation: children` (SPEC §12.2/§12.4, issue #72); see
    `_lib.resolve_root_generation`.
    """
    return build_ahnentafel_map(conn, root_pid, sex_gaps, root_position=root_position)


def _person_label_from_db(conn: sqlite3.Connection, pid: str) -> str:
    """`Ada Hartley (P-…)` for a message, or the bare P-id when nothing names her.

    The twin of lint's `_person_label`, and it exists for the same reason: a
    person whose record carries no `name:`, or an empty one, would otherwise be
    addressed as `None` in a message about her own family.
    """
    row = conn.execute('SELECT name FROM persons WHERE id = ?', (pid,)).fetchone()
    name = str((row['name'] if row else '') or '').strip()
    return f'{name} ({fmt_id_display(pid)})' if name else fmt_id_display(pid)


def _check_w127_root_anchor(
    conn: sqlite3.Connection, root_pid: str
) -> list[dict]:
    """W127: `root_person` has a genetic child on record, so it is anchored a
    generation too high (SPEC §12.2, issue #70). The lint twin lives in
    `lint._check_ahnentafel_placement`; this is the views half.

    It belongs HERE, not only in lint, because this command is the one that
    acts on the derivation: `--fix` and `--realign` rename and renumber the
    whole promoted tree from the walk seeded at `root_person`. Seeded one rung
    too high, every folder it writes is one generation high, and no other check
    in this pass can object - W103, W110 and W119 all compare the folders
    against the numbers this same walk produced, so they are satisfied exactly
    when the tree has been renumbered wrong consistently. A human who only ever
    runs `fha views brackets` would never be told.

    Reads the relationships table with the identical genetic-only filter
    `_lib.build_ahnentafel_map` walks with (SPEC §12.2: a social or legal
    nature - adoptive, step, foster, guardian, surrogate-gestational, social -
    is never numbered, so it never trips this either; an unset or unrecognised
    nature defaults to genetic). The table is derived from accepted,
    non-negated claims, so a suggested or researched-absence child claim is
    silent here for the same reason it is silent in the numbering.

    Report-only, one finding, and never a rename: the repair is one line in
    `fha.yaml`, which no tool may write on a guess.

    The caller skips calling this entirely when `root_generation: children`
    is set (SPEC §12.2/§12.4, issue #72): that field says outright that
    root_person having a child on record is deliberate - their children are
    #1, collectively - so there is nothing here for this function to see
    that isn't already the intended shape.
    """
    social = sorted(SOCIAL_PARENT_SUBTYPES)
    social_ph = ','.join('?' * len(social))
    rows = conn.execute(
        f"""
        SELECT DISTINCT r.other_id AS pid
        FROM relationships r
        LEFT JOIN claims c ON r.claim_id = c.id
        WHERE r.person_id = ? AND r.rel = 'child'
          AND COALESCE(LOWER(c.subtype), '') NOT IN ({social_ph})
        ORDER BY r.other_id
        """,
        (root_pid, *social),
    ).fetchall()
    if not rows:
        return []
    extra = '' if len(rows) == 1 else f' (and {len(rows) - 1} more)'
    return [{
        'code': 'W127',
        'pid': root_pid,
        'child_pid': rows[0]['pid'],
        'msg': (
            f'W127 fha.yaml: root_person {_person_label_from_db(conn, root_pid)} has a '
            f'child on record - {_person_label_from_db(conn, rows[0]["pid"])}{extra}. '
            'SPEC §12.2 anchors #1 at the youngest generation, so every couple folder '
            'below derives one generation high - including anything `--fix` or '
            '`--realign` writes from here. Fix it by adding `root_generation: children` '
            'to fha.yaml (root_person stays who it is; their children become #1, '
            'collectively, SPEC §12.2) - or by re-anchoring root_person to a child '
            'directly - then run `fha index` and `fha views brackets --realign` - or '
            'leave this as-is if anchoring at yourself is deliberate.'
        ),
    }]


def _check_w103_brackets(
    conn: sqlite3.Connection, archive_root: Path
) -> list[dict]:
    """Derive expected bracket lists and return stale-folder-name findings.

    For each couple folder, the expected bracket list is: given names of ALL
    children of persons in the folder, sorted alphabetically, derived from the
    relationships table (which mirrors accepted parent/child claims, identified
    by their roles: map regardless of nature). A child who joined other than by
    birth is marked `(adopted)`/`(step)`/… via the backing claim's subtype, so an
    adopted child is shown but visibly distinct (SPEC §12.2).

    The same pass also proposes adding the missing `+ second spouse` half of
    the folder's base name (SPEC §12.2's illustrated convention) - see
    `_spouse_extended_base` for the deliberately conservative add-only rule.

    Each returned dict has keys: code, folder, old_name, new_name, msg.

    WHY ALL CHILDREN (including direct-line): The bracket list is a human
    convenience label showing every child of the couple.  Direct-line children
    (who also have their own numbered couple folder) still appear in their
    parents' bracket list - e.g. Calvin appears in folder 040 even though
    Calvin's own couple folder is 020.  This matches the observed example-archive
    pattern (Warren in 020's brackets, Edith in 010's, etc.).
    """
    issues = []
    for folder in _couple_folder_dirs(archive_root):
        folder_name = folder.name
        current_names = _parse_bracket_names(folder_name)

        person_ids = _persons_in_folder(conn, folder)
        if not person_ids:
            continue

        # A stray direct-line child profile may also live in the folder (a W110
        # misplacement that fha views brackets --fix will correct).  Exclude any
        # occupant who is a child of another occupant so that grandchildren do not
        # appear in the bracket list.
        if len(person_ids) > 1:
            pid_set = set(person_ids)
            pl = ','.join('?' * len(person_ids))
            stray_ids = {
                r[0] for r in conn.execute(
                    f'SELECT other_id FROM relationships '
                    f'WHERE person_id IN ({pl}) AND rel = "child"',
                    person_ids,
                ).fetchall()
            } & pid_set
            if stray_ids:
                person_ids = [p for p in person_ids if p not in stray_ids]
        if not person_ids:
            continue

        # All children of all persons in this folder, tracking the parent so
        # grandchildren via stray occupants can be excluded, and the edge nature
        # (from the backing claim) so a non-birth child can be marked.
        placeholders = ','.join('?' * len(person_ids))
        child_rows = conn.execute(
            f"""
            SELECT r.person_id AS parent_pid, r.other_id AS child_pid,
                   p.name AS name, LOWER(COALESCE(c.subtype, '')) AS nature
            FROM relationships r
            JOIN persons p ON r.other_id = p.id
            LEFT JOIN claims c ON r.claim_id = c.id
            WHERE r.person_id IN ({placeholders}) AND r.rel = 'child'
            """,
            person_ids,
        ).fetchall()

        # A stray folder occupant who is also a child here would contribute their
        # own children (grandchildren of the couple) to the bracket.  Exclude any
        # child whose parent_pid itself appears as a child in these results.
        all_result_child_pids = {r['child_pid'] for r in child_rows}
        # Aggregate each child's natures across its edges to folder parents (a pair
        # may carry both a biological and an adoptive edge - the co-valid case); a
        # child with any genetic edge reads as a birth child (bare name), one joined
        # only by a social/legal bond reads `Given (adopted)`. Mirrors
        # lint._check_bracket_lists so both backends derive identical lists.
        child_info: dict[str, dict] = {}
        for r in child_rows:
            if r['parent_pid'] in all_result_child_pids or not r['name']:
                continue
            info = child_info.setdefault(r['child_pid'], {'name': r['name'], 'natures': set()})
            info['natures'].add(r['nature'])
        variants: dict[str, list[str]] = {}
        if child_info:
            vpl = ','.join('?' * len(child_info))
            for r in conn.execute(
                f'SELECT person_id, variant FROM person_variants WHERE person_id IN ({vpl})',
                list(child_info),
            ).fetchall():
                variants.setdefault(r['person_id'], []).append(r['variant'])
        derived_entries = []
        for cpid, info in child_info.items():
            natures = info['natures']
            label = None
            if not any(is_genetic_parent_subtype(s) for s in natures):
                for s in sorted(natures):
                    label = nonbirth_bracket_label(s)
                    if label:
                        break
            derived_entries.append((
                bracket_given_name(info['name']), label,
                [info['name']] + variants.get(cpid, [])))
        # Keeps a still-true hand-written token ("Louise" for Ada Louise) -
        # the shared rule, so views and lint agree on every folder.
        derived_names = reconcile_bracket_entries(derived_entries, current_names)

        base_name = re.sub(r'\s*\[[^\]]*\]', '', folder_name).rstrip()
        new_base, other_name = spouse_extended_base(
            base_name, person_ids,
            {pid: _person_name_from_db(conn, pid) for pid in person_ids})

        bracket_stale = sorted(current_names) != sorted(derived_names)
        spouse_add = new_base != base_name
        if bracket_stale or spouse_add:
            if bracket_stale:
                bracket_part = (
                    f' [{" + ".join(derived_names)}]' if derived_names else ''
                )
            else:
                # Bracket already correct: keep its text byte-for-byte (do not
                # churn a same-set, differently-ordered hand-written list).
                m = re.search(r'(\s*\[[^\]]*\])', folder_name)
                bracket_part = m.group(1) if m else ''
            new_name = new_base + bracket_part
            parts = []
            if spouse_add:
                parts.append(f'couple folder names only one partner - add {other_name}')
            if bracket_stale:
                parts.append(
                    f'stale bracket list '
                    f'[{" + ".join(current_names)}] -> [{" + ".join(derived_names)}]')
            issues.append({
                'code': 'W103',
                'folder': folder,
                'old_name': folder_name,
                'new_name': new_name,
                'msg': f'W103 people/{folder_name}: {"; ".join(parts)}',
            })
    return issues


def _person_couple_folder(
    conn: sqlite3.Connection, archive_root: Path, pid: str
) -> Path | None:
    """Return the couple folder a person's profile currently lives in, or None.

    Looks up the profile path from person_files (indexed) and derives the folder.
    Returns None if the person has no indexed profile, or if the profile is in
    stubs/ or connections/ (which are never couple folders).
    """
    row = conn.execute(
        "SELECT path FROM person_files WHERE person_id = ? AND kind = 'profile'",
        (pid,),
    ).fetchone()
    if not row:
        return None
    parts = row['path'].replace('\\', '/').split('/')
    if len(parts) < 3 or parts[0] != 'people':
        return None
    folder_name = parts[1]
    if folder_name.lower() in ('connections', 'stubs'):
        return None
    return archive_root / 'people' / folder_name


def _person_name_from_db(conn: sqlite3.Connection, pid: str) -> str:
    """Return the display name for pid from the persons table, or pid itself."""
    row = conn.execute("SELECT name FROM persons WHERE id = ?", (pid,)).fetchone()
    return row['name'] if row else pid


def _companion_files_in_folder(folder: Path, pid: str) -> list[Path]:
    """Return all .md companion files for pid that exist in folder on disk.

    Matches files whose stem ends with _{pid} (case-insensitive, e.g.
    'hartley__thomas_P-de957bcda1.md').  This catches profile, research,
    timeline, and draft-queue files regardless of whether they are in the
    SQLite index (generated files carry no frontmatter id and are therefore
    absent from person_files) - and, for a record old enough to still have
    one, a leftover PER-PERSON sources-index file from before #76 (its
    `## Sources` section now travels WITH the profile automatically, since it
    lives inside it - nothing extra to move for a current record).

    WHY DISK SCAN: generated view files (timeline, draft-queue) lack a
    frontmatter `id:` field so index.py does not add them to person_files.
    Querying person_files alone would leave them behind when a W110 file-move
    fix is applied.  Scanning disk is the only reliable way to move all of a
    person's companion files atomically.
    """
    suffix = f'_{pid}'.upper()
    return sorted(
        f for f in folder.iterdir()
        if f.is_file() and f.suffix == '.md'
        and f.stem.upper().endswith(suffix)
    )


def _check_w110_ahnentafel(
    conn: sqlite3.Connection,
    archive_root: Path,
    pid_to_pos: dict[str, int],
) -> list[dict]:
    """Implement TOOLING §7 checks 2 and 3 for Ahnentafel placement.

    Check 2 - folder-number rename:
      For each direct-line couple, the couple folder's numeric prefix must equal
      the even Ahnentafel position of the person in the 'father' slot (pos N,
      where N is even).  If the prefix is wrong, the folder itself is renamed.
      One folder-rename issue is emitted per mismatched folder.

    Check 3 - person-file placement:
      For each direct-line person at position N (N ≥ 2), ALL companion files
      (profile, research, timeline, sources-index, draft-queue) must live in the
      folder whose prefix equals N (if N is even) or N−1 (if N is odd).  Files
      are discovered by a disk scan so that generated companions not present in
      person_files are still found.  Issues are suppressed when the enclosing
      folder is already being renamed by check 2 to the correct prefix.

    Non-direct-line files (connections/, stubs/) are always skipped.
    Each returned dict has keys: code, kind ('folder_rename'|'file_move'), and
    kind-specific fields (old_folder/new_folder or src_path/dst_path/expected_folder).
    """
    issues: list[dict] = []

    # ── Check 2: wrong couple-folder prefix ───────────────────────────────────
    # Per-person iteration misidentifies the problem when a person is accidentally
    # in the wrong folder: if Thomas (pos 40) lands in folder '020 Calvin...', the
    # naive check would propose renaming 020 to 040, corrupting the Calvin folder.
    #
    # The correct invariant: a folder should be renamed only when ALL of the even-
    # position anchors found in it claim the SAME expected prefix, and that prefix
    # differs from the folder's actual prefix.  If two even-position persons from
    # different Ahnentafel branches are both present (e.g. Thomas pos-40 and Calvin
    # pos-20 in folder 020), they claim conflicting prefixes - the folder itself is
    # not misnamed; one person is simply misplaced (check 3 handles that).

    # Step A: collect couple-prefix anchors per folder.
    # Both even- and odd-position persons are included, each mapped to their couple
    # prefix (even stays even; odd uses pos-1).  This lets a folder whose only
    # direct-line occupant is an odd-slot person (e.g. a mother at pos 3 with no
    # spouse profile in that folder) still receive a rename proposal.
    folder_anchors: dict[Path, list[tuple[str, int]]] = {}
    for pid, pos in pid_to_pos.items():
        if pos < 2:
            continue
        couple_prefix = pos if pos % 2 == 0 else pos - 1
        current_folder = _person_couple_folder(conn, archive_root, pid)
        if current_folder is None:
            continue
        folder_anchors.setdefault(current_folder, []).append((pid, couple_prefix))

    # Canonical folders that already exist on disk, keyed by numeric prefix.
    # Needed before Step B so a rename is never proposed onto a prefix that
    # already has its own couple folder (that would split the couple across
    # two same-prefix folders; the stray file should move there instead).
    existing_prefix_folders: dict[int, Path] = {}
    for folder in _couple_folder_dirs(archive_root):
        m = re.match(r'^(\d+) ', folder.name)
        if m:
            existing_prefix_folders[int(m.group(1))] = folder

    # Step B: emit a folder_rename only when anchors unanimously claim one prefix.
    folders_being_renamed: dict[Path, Path] = {}

    for folder, anchors in folder_anchors.items():
        actual_prefix = _folder_numeric_prefix(folder.name)
        if actual_prefix is None:
            continue
        claimed_prefixes = {pos for _, pos in anchors}
        if len(claimed_prefixes) != 1:
            # Conflicting claims - persons from different branches are co-mingled.
            # Leave the folder name alone; check 3 will propose moving the strays.
            continue
        claimed_prefix = next(iter(claimed_prefixes))
        if claimed_prefix == actual_prefix:
            continue
        existing_canonical = existing_prefix_folders.get(claimed_prefix)
        if existing_canonical is not None and existing_canonical != folder:
            # A couple folder for claimed_prefix already exists elsewhere; renaming
            # this folder onto the same prefix would split the couple into two
            # folders. Leave this folder alone - check 3's file-move path will
            # relocate the stray person's files into the existing canonical folder.
            continue

        new_name = re.sub(r'^\d+', str(claimed_prefix).zfill(3), folder.name)
        new_folder = folder.parent / new_name
        folders_being_renamed[folder] = new_folder

        anchor_pid = next(pid for pid, pos in anchors if pos == claimed_prefix)
        person_name = _person_name_from_db(conn, anchor_pid)
        issues.append({
            'code': 'W110',
            'kind': 'folder_rename',
            'old_folder': folder,
            'new_folder': new_folder,
            'msg': (
                f'W110 people/{folder.name}: folder prefix {actual_prefix} '
                f'should be {claimed_prefix} ({person_name}, Ahnentafel {claimed_prefix}); '
                f'rename to {new_name}'
            ),
        })

    # ── Check 3: person files in wrong folder ─────────────────────────────────
    # Only canonical direct-line folders (digits followed by a space) populate
    # the prefix map; suffix folders like '040b Thomas' share the same numeric
    # prefix and would overwrite the canonical entry.
    all_couple_dirs = list(_couple_folder_dirs(archive_root))
    folder_by_prefix: dict[int, Path] = {}
    for folder in all_couple_dirs:
        # Match only canonical folders: digits then a literal space (excludes '040b…').
        m = re.match(r'^(\d+) ', folder.name)
        if m:
            folder_by_prefix[int(m.group(1))] = folder

    # people/connections/ is a real, permanent home for a curated NON-direct
    # person (#80) - but a person filed there can later turn out to be
    # direct-line (a parent link discovered afterwards, or a hand edit): the
    # promote engine's own docstring calls that "a couple-folder mismatch,
    # left to lint (W110)", so this check must actually be able to see it.
    # `all_couple_dirs` deliberately excludes connections/ (it is never a
    # couple folder to rename), so it is scanned here as an ADDITIONAL stray-
    # file source, never as a rename/derivation candidate.
    connections_dir = archive_root / 'people' / 'connections'
    stray_scan_dirs = all_couple_dirs + ([connections_dir] if connections_dir.is_dir() else [])

    # Also register pending W110 rename destinations so that file-move targets
    # resolve to the correct folder even when it doesn't yet exist on disk.
    for new_folder in folders_being_renamed.values():
        m = re.match(r'^(\d+) ', new_folder.name)
        if m:
            folder_by_prefix.setdefault(int(m.group(1)), new_folder)

    for pid, pos in pid_to_pos.items():
        if pos < 2:
            continue
        expected_prefix = pos if pos % 2 == 0 else pos - 1

        profile_folder = _person_couple_folder(conn, archive_root, pid)

        # Determine the destination folder (or derive its name from the profile folder).
        dest_folder = folder_by_prefix.get(expected_prefix)
        if dest_folder is None:
            if profile_folder is None:
                continue
            new_fname = re.sub(r'^\d+', str(expected_prefix).zfill(3), profile_folder.name)
            dest_folder = archive_root / 'people' / new_fname

        person_name = _person_name_from_db(conn, pid)

        # Scan couple dirs (plus connections/, added above) for companion files
        # that belong to this person but are not in the expected folder -
        # catches strays even when the profile itself is already correctly
        # placed.
        for folder in stray_scan_dirs:
            if folder == dest_folder:
                continue
            # If this folder is about to be renamed to the correct prefix, the
            # rename will fix placement; no move needed.
            if folder in folders_being_renamed:
                dest_of_rename = folders_being_renamed[folder]
                if _folder_numeric_prefix(dest_of_rename.name) == expected_prefix:
                    continue
            for src_path in _companion_files_in_folder(folder, pid):
                dst_path = dest_folder / src_path.name
                actual_prefix = _folder_numeric_prefix(folder.name)
                current_location = (f'folder prefix {actual_prefix}' if actual_prefix is not None
                                     else f'people/{folder.name}/')
                issues.append({
                    'code': 'W110',
                    'kind': 'file_move',
                    'src_path': src_path,
                    'dst_path': dst_path,
                    'expected_folder': dest_folder,
                    'msg': (
                        f'W110 people/{folder.name}/{src_path.name}: '
                        f'{person_name} (Ahnentafel {pos}) '
                        f'is in {current_location}, '
                        f'expected prefix {expected_prefix}'
                    ),
                })

    return issues


def _new_couple_folder(archive_root: Path, prefix: int, members: list[dict]) -> Path:
    """The folder a batch promotion invents for couple `prefix` when none exists.

    Named after the even (father-slot) member - for a couple always the lower
    position (N < N+1), for a lone stub simply that person, matching what
    single-person `fha person promote` does. When BOTH partners are in the
    batch, the folder names both from the start (`004 Robert + Jeanne`, the
    SPEC §12.2 convention) - otherwise the fresh folder announces one partner
    and waits for the next W103 pass to add the other. ONE rule, shared by
    W119's preview and --realign's rebase, so the two can never invent
    different names for the same couple.

    A partner whose only "name" is a placeholder (`unknown`, an unnamed stub)
    is left off, and that applies to BOTH slots: a folder name is for humans,
    and W103's add-only spouse rule never touches a base that already carries a
    `+`, so `004 unknown + Jeanne` would keep the placeholder forever once the
    even-slot person is finally named. When only the odd-slot partner has a real
    name, that name becomes the temporary base (`004 Jeanne`) - out of the
    conventional order, but a base W103 can repair into `004 Jeanne + Robert`
    later. Only when neither slot has a name does the placeholder go in, which
    is exactly what promoting a single unnamed stub produces today.
    """
    namer = min(members, key=lambda c: c['pos'])
    partner = next((c for c in members if c['pos'] == namer['pos'] + 1), None)
    # A name made only of characters a folder can't hold ("???") counts as a
    # placeholder: its safe form is empty and would leave "002 Thomas + ".
    named = [safe for safe in (folder_name_text(c['name']) for c in (namer, partner)
                               if c is not None and not is_placeholder_name(c['name']))
             if safe]
    label = (' + '.join(named) if named
             else folder_name_text(namer['name']) or 'unknown')
    return archive_root / 'people' / f'{str(prefix).zfill(3)} {label}'


def _check_w119_direct_line_stubs(
    conn: sqlite3.Connection,
    archive_root: Path,
    pid_to_pos: dict[str, int],
    generations: int | None = None,
) -> list[dict]:
    """Check 4 (W119): direct-line ancestors still filed as stubs.

    Exactly the people the W110 machinery deliberately skips: a person with a
    DERIVED Ahnentafel position >= 2 whose record is `tier: stub` or still
    lives under people/stubs/. Report-only here - promotion is always an
    explicit act (`--fix-promote`'s previewed gate, or `fha person promote`),
    never a silent side effect (SPEC §4: curiosity-driven graduation). These
    are leads, not defects: a stub is a legitimate permanent state, and the
    wording must read that way.

    `generations` narrows to positions within N generations of the root
    (parents are generation 1); None means the whole derived line. Each issue
    dict carries pid/pos/name/dest_folder so `--fix-promote` can apply the
    promote engine without re-deriving anything.

    WHY DESTINATIONS ARE RESOLVED IN A SECOND PASS: both partners of a couple
    share ONE couple folder (SPEC §12.2). When BOTH sit at their couple's
    positions (the even father-slot N and the odd mother-slot N+1) AND that
    folder does not exist on disk yet, deriving each destination independently
    against the same pre-promotion state would invent two DIFFERENT new folders
    (`002 Father` and `002 Mother`), and a batch `--fix-promote` would create
    both and split the couple. So candidates are gathered first, then grouped by
    couple prefix: every member of a prefix gets the SAME destination - the
    existing couple folder if one is on disk, otherwise one new folder named
    after the even (father-slot) member. That is exactly the folder a normal
    one-at-a-time `fha person promote` produces (it names a new couple folder
    after the person being promoted; done father-first, the mother then finds
    that folder), so promoted couples land where normally-created couples do.
    The shared destination is baked into every issue dict, so the dry-run
    preview and the live apply describe the identical folder.
    """
    # ── Pass 1: gather the stub candidates (destination deferred to pass 2) ──
    candidates: list[dict] = []
    for pid, pos in sorted(pid_to_pos.items(), key=lambda kv: kv[1]):
        if pos < 2:
            continue
        if generations is not None and ahnentafel_generation(pos) > generations:
            continue
        row = conn.execute(
            'SELECT name, tier, status, path FROM persons WHERE id = ?', (pid,),
        ).fetchone()
        if row is None:
            continue   # referenced but recordless (the fha stubs / E005 set)
        if str(row['status'] or '').lower() == 'merged':
            continue   # tombstones resolve through merged_into, never promote
        rel_path = (row['path'] or '').replace('\\', '/')
        parts = rel_path.split('/')
        in_stubs = len(parts) >= 3 and parts[0] == 'people' and parts[1].lower() == 'stubs'
        is_stub_tier = str(row['tier'] or 'stub').lower() != 'curated'
        if not (is_stub_tier or in_stubs):
            continue
        candidates.append({
            'pid': pid,
            'pos': pos,
            'name': row['name'] or fmt_id_display(pid),
            'path': rel_path,
            'prefix': couple_folder_prefix(pos),
        })

    # ── Pass 2: one shared destination folder per couple prefix ─────────────
    shared_dest: dict[int, Path] = {}
    for prefix in {c['prefix'] for c in candidates}:
        existing = couple_folder_for_prefix(archive_root, prefix)
        if existing is not None:
            shared_dest[prefix] = existing
            continue
        # No folder on disk: name the new one after the even (father-slot)
        # member. For a couple that is always the lower position (N < N+1), and
        # for a lone stub it is simply that person - matching single-person
        # promote, which names the new folder after whoever is promoted. When
        # BOTH partners are in the batch, name the folder for both from the
        # start (`004 Robert + Jeanne`, the SPEC §12.2 convention) - otherwise
        # the fresh folder announces only one partner and waits for the next
        # W103 pass to add the other.
        members = [c for c in candidates if c['prefix'] == prefix]
        shared_dest[prefix] = _new_couple_folder(archive_root, prefix, members)

    issues: list[dict] = []
    for c in candidates:
        issues.append({
            'code': 'W119',
            'pid': c['pid'],
            'pos': c['pos'],
            'name': c['name'],
            'path': c['path'],
            'dest_folder': shared_dest[c['prefix']],
            'msg': (
                f'W119 {c["path"]}: {c["name"]} (Ahnentafel {c["pos"]}) is a '
                'direct-line ancestor still filed as a stub - promote with '
                f'`fha person promote {fmt_id_display(c["pid"])}` or '
                '`fha views brackets --fix-promote`'
            ),
        })
    return issues


def _print_promote_preview(w119: list[dict], archive_root: Path) -> None:
    """Print the plain-words promotion plan for every W119 candidate.

    Each candidate's plan comes from the promote engine's own dry run, so the
    preview and the apply can never describe different writes. A candidate
    whose plan cannot be computed (record vanished since the index was built,
    unreadable file) previews as a SKIP with the reason.
    """
    print('\nStub promotions (W119):')
    for item in w119:
        label = f"{item['name']} [{fmt_id_display(item['pid'])}]"
        record_path = find_person_record_path(archive_root, item['pid'])
        if record_path is None:
            print(f'  SKIP  {label}: no record file found on disk - run `fha index`.')
            continue
        try:
            plan = promote_person_record(
                archive_root, item['pid'], record_path, item['dest_folder'],
                dry_run=True)
        except PromotionError as e:
            print(f'  SKIP  {label}: {e}')
            continue
        print(f'  PROMOTE  {label} (Ahnentafel {item["pos"]}):')
        for step in plan['steps']:
            print(f'      - {step}')


def _apply_promotions(w119: list[dict], archive_root: Path) -> tuple[int, int]:
    """Apply the promote engine to every W119 candidate; return (applied, failures).

    Each promotion is transactional on its own (the engine rolls back its
    steps on failure), so one failed person never leaves the others half
    done - partial success across the batch is reported per person and in the
    caller's exit code, mirroring `_apply_bracket_fixes`.
    """
    applied = failures = 0
    for item in w119:
        label = f"{item['name']} [{fmt_id_display(item['pid'])}]"
        record_path = find_person_record_path(archive_root, item['pid'])
        if record_path is None:
            print(f'  ERROR {label}: no record file found on disk - skipped.',
                  file=sys.stderr)
            failures += 1
            continue
        try:
            plan = promote_person_record(
                archive_root, item['pid'], record_path, item['dest_folder'])
        except PromotionError as e:
            print(f'  ERROR {label}: {e}', file=sys.stderr)
            failures += 1
            continue
        applied += 1
        print(f'  PROMOTED  {label}:')
        for step in plan['steps']:
            print(f'      - {step}')
    return applied, failures


def _realign_promotion_dests(
    w119: list[dict], rename_map: dict[Path, Path], archive_root: Path,
    created_folders: list[Path] | None = None,
) -> tuple[int, str | None]:
    """Point every W119 promotion at the couple folder as named AFTER the renames.

    --realign applies the W103/W110 renames before the promotions, but the
    W119 destinations were derived against the CURRENT folder tree. Following
    the rename blindly would be wrong in one direction, ignoring it wrong in
    the other:
      - a folder about to gain a DIFFERENT numeric prefix (a W110 rename) was
        misnamed - that is why it is being renamed - so it must stop
        attracting the promotions computed against its old prefix;
      - a folder renamed with its prefix intact (a W103 bracket/spouse
        refresh) is still the couple's folder and must carry its promotions
        with it.
    So destinations are recomputed per couple prefix against the post-rename
    name set: exactly one post-rename folder carries the prefix -> that
    folder; none does -> a fresh couple folder named for the batch members
    (the same invention rule as _check_w119_direct_line_stubs, even-slot
    partner first); two or more would share it -> refuse rather than file
    records into an arbitrary half.

    Mutates each issue's dest_folder in place. Returns (changed_count,
    problem): problem is None on success, else the plain-words refusal.
    """
    # The post-fix tree: today's couple folders after the pending renames,
    # PLUS the folders a W110 file_move will create (a misfiled record whose
    # expected folder does not exist yet - _apply_bracket_fixes mkdirs it).
    # Without those, a promotion for the same prefix would invent a second,
    # differently-named folder beside the one the move is about to create.
    post_folders = [rename_map.get(f, f) for f in _couple_folder_dirs(archive_root)]
    for extra in created_folders or []:
        if extra not in post_folders:
            post_folders.append(extra)
    by_prefix: dict[int, list[Path]] = {}
    for post in post_folders:
        m = re.match(r'^(\d+) ', post.name)
        if m:
            by_prefix.setdefault(int(m.group(1)), []).append(post)

    members_by_prefix: dict[int, list[dict]] = {}
    for item in w119:
        members_by_prefix.setdefault(
            couple_folder_prefix(item['pos']), []).append(item)

    changed = 0
    for prefix, members in sorted(members_by_prefix.items()):
        posts = by_prefix.get(prefix, [])
        if len(posts) > 1:
            return changed, (
                f'two folders would share the couple prefix {prefix:03d} '
                f'after the renames above ({" and ".join(p.name for p in posts)}) '
                '- the promotions cannot pick one safely. Apply the renames '
                'alone first (`fha views brackets --fix`), resolve the '
                'duplicate prefix, then re-run.')
        if posts:
            dest = posts[0]
        else:
            dest = _new_couple_folder(archive_root, prefix, members)
        for item in members:
            if item['dest_folder'] != dest:
                item['dest_folder'] = dest
                changed += 1
    return changed, None


def _compose_folder_renames(
    w103: list[dict], w110: list[dict]
) -> tuple[list[dict], list[dict], list[dict]]:
    """Compose W103 bracket renames and W110 folder renames that share a source folder.

    When the same folder appears in both a W103 bracket-list rename and a W110
    folder-number rename, applying them sequentially would fail: the W103 rename
    changes the folder's path, so the W110 rename's old_folder no longer exists.

    Instead, compose both changes into a single rename:
      - numeric prefix from the W110 rename
      - bracket list from the W103 rename (replacing whatever bracket W110 derived)

    The composed rename replaces the W110 item's new_folder; the W103 item is
    dropped (its change is now baked into the W110 rename).

    Additionally, any remaining W103 item whose folder has outgoing W110 file_moves
    is suppressed and returned as the third tuple element.  The bracket for such a
    folder was computed with stale occupancy (the misplaced person was counted as an
    occupant) and will be wrong after the moves; suppress now, recompute next run.

    For surviving W103 items, W110 file_move src_paths that point inside the
    folder being renamed are rewritten so that the src_path stays valid after the
    W103 rename executes first in _apply_bracket_fixes.

    Returns (w103, w110, w103_suppressed).  All three lists are modified in-place.
    """
    w110_folder_renames: dict[Path, dict] = {
        item['old_folder']: item
        for item in w110
        if item['kind'] == 'folder_rename'
    }

    w103_drop = []
    for item in w103:
        old_folder = item['folder']
        if old_folder not in w110_folder_renames:
            continue

        w110_item = w110_folder_renames[old_folder]
        # Target the W110 rename was pointing at *before* composition. File-move
        # destinations were computed against this name, so we must rebase them once
        # the rename target changes below.
        old_w110_target = w110_item['new_folder']

        # Compose: W110 contributes only the corrected numeric prefix; the rest
        # of the name (base, any spouse half W103 just added, bracket list)
        # comes from the W103 target. Substituting the prefix into the W103
        # name - rather than grafting W103's bracket onto W110's base - keeps
        # a `+ second spouse` addition from being silently dropped.
        prefix_m = re.match(r'^(\d+)', old_w110_target.name)
        if prefix_m:
            composed_name = re.sub(r'^\d+', prefix_m.group(1), item['new_name'])
        else:   # W110 target unexpectedly prefix-less: keep the W103 name whole
            composed_name = item['new_name']
        composed_folder = old_w110_target.parent / composed_name

        old_msg = w110_item['msg']
        w110_item['new_folder'] = composed_folder
        w110_item['msg'] = (
            re.sub(r'; rename to .*', '', old_msg)
            + f'; rename to {composed_name} (prefix + bracket update)'
        )

        # File-move destinations were computed against the pre-composition target
        # (old_w110_target, e.g. '040 Family [old]'). Without this rebase, --fix
        # would rename the folder to the composed name but move companion files into
        # old_w110_target, creating a second, stale couple folder.
        for fm in w110:
            if fm['kind'] != 'file_move':
                continue
            fm['dst_path']        = _rebase(fm['dst_path'],        old_w110_target, composed_folder)
            fm['expected_folder'] = _rebase(fm['expected_folder'], old_w110_target, composed_folder)

        w103_drop.append(item)

    for item in w103_drop:
        w103.remove(item)

    # Suppress W103 for folders that have outgoing W110 file_moves.
    # Those brackets were computed with stale occupancy (the misplaced person
    # was still in the folder at check time). After the moves, the bracket will
    # differ. Skip the rename now; a fresh run after applying W110 will produce
    # the correct bracket.
    w110_src_folders = {
        item['src_path'].parent
        for item in w110
        if item['kind'] == 'file_move'
    }
    w103_suppressed = [item for item in w103 if item['folder'] in w110_src_folders]
    for item in w103_suppressed:
        w103.remove(item)

    # For remaining W103-only renames, update any W110 file_move src_paths
    # that point inside the folder being renamed.
    # _apply_bracket_fixes runs W103 renames first, so without this update
    # the W110 file-move sources would point at the pre-rename folder path
    # and the moves would silently skip.
    for item in w103:
        old_folder = item['folder']
        new_folder = old_folder.parent / item['new_name']
        for w110_item in w110:
            if w110_item['kind'] != 'file_move':
                continue
            w110_item['src_path']       = _rebase(w110_item['src_path'],       old_folder, new_folder)
            w110_item['dst_path']       = _rebase(w110_item['dst_path'],       old_folder, new_folder)
            w110_item['expected_folder'] = _rebase(w110_item['expected_folder'], old_folder, new_folder)

    return w103, w110, w103_suppressed


def _print_bracket_preview(
    w103: list[dict], w110: list[dict], archive_root: Path
) -> None:
    """Print a human-readable preview of all planned renames and moves."""
    if w103:
        print('\nBracket list renames (W103):')
        for item in w103:
            print(f'  RENAME  people/{item["old_name"]}')
            print(f'       ->  people/{item["new_name"]}')
    if w110:
        folder_renames = [i for i in w110 if i['kind'] == 'folder_rename']
        file_moves = [i for i in w110 if i['kind'] == 'file_move']
        if folder_renames:
            print('\nAhnentafel folder renames (W110):')
            for item in folder_renames:
                try:
                    old_rel = item['old_folder'].relative_to(archive_root)
                    new_rel = item['new_folder'].relative_to(archive_root)
                except ValueError:
                    old_rel = item['old_folder']
                    new_rel = item['new_folder']
                print(f'  RENAME  {old_rel}')
                print(f'       ->  {new_rel}')
        if file_moves:
            print('\nPerson file moves (W110):')
            for item in file_moves:
                try:
                    src_rel = item['src_path'].relative_to(archive_root)
                    dst_rel = item['dst_path'].relative_to(archive_root)
                except ValueError:
                    src_rel = item['src_path']
                    dst_rel = item['dst_path']
                print(f'  MOVE  {src_rel}')
                print(f'      -> {dst_rel}')


def _planned_folder_renames(
    w103: list[dict], w110: list[dict]
) -> dict[Path, Path]:
    """Every folder rename the fix pass will perform: source -> destination."""
    renames = {item['folder']: item['folder'].parent / item['new_name']
               for item in w103}
    for item in w110:
        if item['kind'] == 'folder_rename':
            renames[item['old_folder']] = item['new_folder']
    return renames


def _location_before_renames(p: Path, renames: dict[Path, Path]) -> Path:
    """Where a post-rename path sits on disk right NOW, before any rename runs.

    File-move sources and destinations are expressed against the folder names
    the fix pass will have created by the time the moves run. To ask "is
    something already sitting there?" before the first mutation, the path has
    to be read back through the pending renames: `040 Family [new]/kin.md` is
    at this moment `040 Family [old]/kin.md`.
    """
    for old, new in renames.items():
        try:
            return old / p.relative_to(new)
        except ValueError:
            continue
    return p


def _bracket_fix_conflicts(
    w103: list[dict], w110: list[dict], archive_root: Path
) -> list[str]:
    """Everything that would stop this plan applying as one piece, in plain words.

    ONE preflight for the whole pass - folders AND files. Checking the folder
    renames alone was not enough: the renames were applied, then a file whose
    destination already held a duplicate companion was skipped, and in
    --realign every promotion after it was skipped too. The command is
    presented as one whole-tree realignment, so a taken destination must stop
    it BEFORE the first rename, not halfway through.

    A file-move destination is compared against the tree as it stands today
    (`_location_before_renames`), because a folder rename earlier in the plan
    carries whatever is inside it to the destination name. Two moves landing on
    one path count as a conflict for the same reason: the second would be
    skipped after the first had already been applied.

    The last check is not a taken destination but the same partial state by
    another route: renames that leave TWO couple folders carrying one number
    (a couple filed in two separate folders, each claiming the number from its
    own occupant). Applying those produces exactly the duplicate-prefix tree
    the next bracket run refuses outright, so it is caught here, before
    anything moves, and named as the human problem it is.
    """
    conflicts: list[str] = []
    renames = _planned_folder_renames(w103, w110)

    for old_folder, new_folder in renames.items():
        if new_folder != old_folder and new_folder.exists():
            conflicts.append(
                'rename destination already exists: '
                f'{_rel_display(new_folder, archive_root)}')

    def _prefix_of(name: str) -> int | None:
        m = re.match(r'^(\d+) ', name)   # canonical folders only ('004b …' is its own)
        return int(m.group(1)) if m else None

    before: dict[int, int] = {}
    after: dict[int, list[str]] = {}
    for folder in _couple_folder_dirs(archive_root):
        post = renames.get(folder, folder)
        pre_prefix = _prefix_of(folder.name)
        if pre_prefix is not None:
            before[pre_prefix] = before.get(pre_prefix, 0) + 1
        post_prefix = _prefix_of(post.name)
        if post_prefix is not None:
            after.setdefault(post_prefix, []).append(
                folder.name if post == folder
                else f'{folder.name} -> {post.name}')
    for prefix, labels in sorted(after.items()):
        # Only a duplicate this plan would CREATE. A tree that already had two
        # folders on one number keeps whatever it had: refusing there would
        # block an unrelated bracket refresh over a pre-existing condition the
        # Ahnentafel checks already report in their own words.
        if len(labels) > 1 and before.get(prefix, 0) < 2:
            conflicts.append(
                f'{" and ".join(sorted(labels))} would both end up as couple '
                f'{prefix:03d} - one couple, one folder. Merge those folders '
                'by hand (or give the non-ancestral one a letter suffix, e.g. '
                f'"{prefix:03d}b …"), then re-run')

    claimed: dict[Path, Path] = {}
    for item in w110:
        if item['kind'] != 'file_move':
            continue
        src_now = _location_before_renames(item['src_path'], renames)
        dst_now = _location_before_renames(item['dst_path'], renames)
        if dst_now != src_now and dst_now.exists():
            conflicts.append(
                'a file is already at the destination '
                f'{_rel_display(item["dst_path"], archive_root)} '
                f'(moving {_rel_display(item["src_path"], archive_root)}) '
                '- move or delete one of them, then re-run')
        elif dst_now in claimed:
            conflicts.append(
                'two files would be moved to the same place, '
                f'{_rel_display(item["dst_path"], archive_root)} '
                f'({_rel_display(claimed[dst_now], archive_root)} and '
                f'{_rel_display(item["src_path"], archive_root)})')
        else:
            claimed[dst_now] = item['src_path']
    return conflicts


def _apply_bracket_fixes(
    w103: list[dict], w110: list[dict], archive_root: Path
) -> tuple[int, bool]:
    """Apply renames and moves collected by the check functions.

    A preflight (`_bracket_fix_conflicts`) first checks every destination the
    pass will write to - folder renames AND file moves - plus the couple
    numbers the renames would leave doubled up. On any of those, NOTHING is
    mutated and the function returns (problem_count, aborted=True) so the
    caller can fail before any partial change.

    Otherwise it renames folders (os.rename) and moves person files
    (shutil.move), creating destination folders as needed. Folder-rename
    failures and file-move skips are both counted as `failures`.

    Returns (failures, aborted). Callers treat aborted as EXIT_FAILURE and any
    nonzero `failures` as EXIT_WARNINGS - a non-empty fix run must never report
    clean after a rename/move could not be applied.
    """
    # ── Preflight: every destination conflict, before the first mutation ──────
    # The callers run this same check before the Apply? gate; repeating it here
    # is the last-moment guard (the tree can change between preview and apply)
    # and keeps the engine safe for any future caller that forgets.
    conflicts = _bracket_fix_conflicts(w103, w110, archive_root)
    if conflicts:
        _print_fix_conflicts(conflicts)
        return len(conflicts), True

    failures = 0

    # W103: folder renames
    for item in w103:
        old_path = item['folder']
        new_path = old_path.parent / item['new_name']
        try:
            os.rename(old_path, new_path)
            print(f'  RENAMED  {item["old_name"]} -> {item["new_name"]}')
        except OSError as e:
            print(f'  ERROR renaming {item["old_name"]}: {e}', file=sys.stderr)
            failures += 1

    # W110: folder renames first, then file moves.
    # Folder renames must precede file moves so that src_path references to
    # files inside an about-to-be-renamed folder are still valid.
    # After each rename succeeds, rewrite src_path for any file_move items
    # whose source lives inside the just-renamed folder.
    file_moves = [item for item in w110 if item['kind'] == 'file_move']
    for item in w110:
        if item['kind'] != 'folder_rename':
            continue
        old_folder = item['old_folder']
        new_folder = item['new_folder']
        try:
            os.rename(old_folder, new_folder)
            try:
                old_rel = old_folder.relative_to(archive_root)
                new_rel = new_folder.relative_to(archive_root)
            except ValueError:
                old_rel, new_rel = old_folder, new_folder
            print(f'  RENAMED  {old_rel} -> {new_rel}')
            for other in file_moves:
                other['src_path']        = _rebase(other['src_path'],        old_folder, new_folder)
                other['dst_path']        = _rebase(other['dst_path'],        old_folder, new_folder)
                other['expected_folder'] = _rebase(other['expected_folder'], old_folder, new_folder)
        except OSError as e:
            print(f'  ERROR renaming {old_folder.name}: {e}', file=sys.stderr)
            failures += 1

    for item in file_moves:
        src = item['src_path']
        dst = item['dst_path']
        expected_folder = item['expected_folder']
        if not src.exists():
            print(f'  SKIP (already moved?) {src}', file=sys.stderr)
            failures += 1
            continue
        if dst.exists():
            print(f'  ERROR {src.name}: destination already exists at {dst} - skipped to avoid overwrite.', file=sys.stderr)
            failures += 1
            continue
        try:
            expected_folder.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            try:
                rel_src = src.relative_to(archive_root)
                rel_dst = dst.relative_to(archive_root)
            except ValueError:
                rel_src, rel_dst = src, dst
            print(f'  MOVED  {rel_src} -> {rel_dst}')
        except OSError as e:
            print(f'  ERROR moving {src}: {e}', file=sys.stderr)
            failures += 1

    return failures, False


def _drop_index_cache(
    archive_root: Path,
    conn: sqlite3.Connection | None,
    applied_sentence: str,
) -> tuple[list[str], str | None]:
    """Drop .cache/index.sqlite after a fix pass moved records around.

    Renames, moves and promotions all change a record's PATH without touching
    its mtime, so `newest_record_mtime` cannot tell that the index now points at
    the old locations: the cache has to go. All three fix paths (--fix,
    --fix-promote, --realign) need exactly this, so the delete, the open handle
    it must release first, and the could-not-delete message live here once.

    `applied_sentence` names what already landed on disk - that work is done and
    irreversible; only the cache drop failed - so the warning can open with it.
    Returns (changed, warning): warning is None on success, otherwise the whole
    plain-words message the caller prints to stderr before exiting with
    warnings. No traceback ever reaches the human.
    """
    if conn is not None:
        conn.close()
    db_path = archive_root / '.cache' / 'index.sqlite'
    try:
        db_path.unlink(missing_ok=True)
    except OSError as exc:
        reason = exc.strerror or str(exc)
        return [], (
            f'\n{applied_sentence}, but the search index cache could not be '
            f'cleared ({reason}): {db_path}. Those records moved without '
            'changing their file times, so the leftover index still points at '
            'the old locations and can look up to date - searches may quietly '
            f'go stale until you rebuild it. Delete {db_path} (or just run '
            '`fha index`) to rebuild the index and clear the stale entries.'
        )
    return [str(db_path)], None


def _print_fix_conflicts(conflicts: list[str]) -> None:
    """Report everything that blocks the fix pass from running as one piece.

    Printed instead of the Apply? gate, and printed identically under
    --dry-run: a plan that cannot run must never be offered for confirmation,
    and the preview must say the same thing the apply would.
    """
    print(
        f'\nERROR: this plan cannot be applied in one piece - '
        f'{len(conflicts)} problem(s), so nothing was changed:',
        file=sys.stderr,
    )
    for line in conflicts:
        print(f'  - {line}', file=sys.stderr)
    print(
        'Fix what each line names, run `fha index`, then re-run this command.',
        file=sys.stderr,
    )


def _confirm_apply(yes: bool) -> bool:
    """
    The Apply? [y/N] gate shared by --fix, --fix-promote and --realign.

    `yes` (the --yes flag, #38) skips the prompt after saying so - the
    preview above it is still printed in full, so a scripted or agent-driven
    run leaves the same audit trail an interactive one does. Without it, a
    non-interactive shell reads EOF here and aborts, which is safe but makes
    the fix flags unusable from scripts and harnesses whose stdin is closed
    (the only way through was piping a literal 'y'). EOF still declines: a
    tree mutation must never proceed on a missing answer.
    """
    if yes:
        print('\nApplying (--yes given, prompt skipped).')
        return True
    try:
        return input('\nApply? [y/N] ').strip().lower() == 'y'
    except EOFError:
        return False


def run_brackets(
    archive_root: Path,
    fix: bool = False,
    dry_run: bool = False,
    fix_promote: bool = False,
    generations: int | None = None,
    realign: bool = False,
    yes: bool = False,
) -> Result:
    """Run the bracket/Ahnentafel checks and (with a fix flag) apply them; return a Result.

    Four checks in one pass (TOOLING §7):
      1. W103 - refresh stale bracket lists, and add the missing `+ second
                 spouse` half of a couple-folder name (add-only, never guessed).
      2. W110 check 2 - rename couple folders whose numeric prefix disagrees
                         with the Ahnentafel-derived number.  (Requires root_person.)
      3. W110 check 3 - move all companion files (profile, research, timeline,
                         draft-queue, and a per-person sources-index file if
                         one still exists from before #76) to the correct
                         folder. (Requires root_person.)
      4. W119 - direct-line ancestors (derived position >= 2) still filed as
                 stubs (`tier: stub`, or a record under people/stubs/).
                 (Requires root_person.  `generations` caps the depth.)
    Plus two report-only notes, both about the DERIVATION rather than the
    folders, so no fix flag touches either:
      * W120 - a placement the derivation made by default (a lone linked parent
                with no recorded `sex:` takes the father/even slot); the human
                records `sex:` to settle it.
      * W127 - `root_person` has a genetic child on record, so the walk that
                feeds checks 1-4 starts a generation too high and every number
                it produces is off by one (#70). The four checks above cannot
                see it - they only verify the folders match these same numbers -
                and `--fix`/`--realign` would write the wrong ones to disk. The
                human re-anchors `root_person` in `fha.yaml`, reindexes, and
                re-runs with `--realign`.

    Without a fix flag or --dry-run: report only (W119 included - a lead, not
    a defect).  --dry-run: findings + full preview, exit without writing.
    --fix: preview the W103/W110 renames/moves, prompt Apply? [y/N], write.
    --fix-promote: preview the W119 promotions (each plan computed by the
    promote engine's own dry run), prompt under the SAME Apply? gate, then
    batch-apply `_lib.promote_person_record` - the one sanctioned tool move
    out of people/stubs/.  The two fix flags are refused together: a W110
    folder rename would invalidate the promotion destinations computed in the
    same pass, so they run as two consecutive invocations instead.
    --realign: the whole-tree recovery verb - ONE combined preview of both
    halves (the W103/W110 renames/moves AND the W119 promotions, the latter
    with destinations recomputed against the post-rename tree, see
    _realign_promotion_dests), one Apply? [y/N] gate, then fixes first and
    promotions second. Refused with --fix/--fix-promote (it subsumes both).
    This is the one-command answer whenever the derived tree may have
    drifted from the promoted folders - a re-anchored `root_person`, a
    corrected `sex:`, a new parent claim, a merge (SPEC §12.2).

    All three fix flags share one Apply? [y/N] gate (_confirm_apply); `yes`
    (--yes) skips the prompt for scripted/non-interactive runs, with the full
    preview still printed (#38).

    Every rename/move path preflights ALL of its destinations first - folder
    renames and file moves together (_bracket_fix_conflicts) - and refuses the
    whole plan, before the gate, when any is already taken. --dry-run prints
    the identical refusal. Checking only the folders would rename them, skip
    the blocked file move, and (under --realign) skip every promotion after
    it: a partial state from a command presented as one whole-tree pass.

    The findings, preview, prompt, and per-rename narration stay inline - the
    interactive Apply? gate is bound to that output and is out of scope to move
    (a deferred Phase-3 concern).  The Result records the outcome: the issue
    counts in `data` and, on a successful fix, the dropped index cache in
    `changed`.
    """
    if fix and fix_promote:
        print(
            'ERROR: --fix and --fix-promote cannot run in one pass - a folder '
            'rename would invalidate the promotion destinations computed with '
            'it. Run `fha views brackets --fix` first, `fha index`, then '
            '`fha views brackets --fix-promote` - or run both halves in one '
            'previewed pass with `fha views brackets --realign`.',
            file=sys.stderr,
        )
        return _views_result(EXIT_FAILURE)
    if realign and (fix or fix_promote):
        print(
            'ERROR: --realign already runs both the rename/move fixes and the '
            'stub promotions in one previewed pass - drop the extra '
            '--fix/--fix-promote flag.',
            file=sys.stderr,
        )
        return _views_result(EXIT_FAILURE)
    if generations is not None and generations < 1:
        print(
            'ERROR: --generations takes a positive number of generations '
            '(1 = parents, 2 = grandparents, …).',
            file=sys.stderr,
        )
        return _views_result(EXIT_FAILURE)

    # A fix run mutates the tree, so it must never run from a stale index;
    # report-only and --dry-run are read-only and tolerate staleness with a
    # warning.
    conn = open_index_db(archive_root, ('persons',),
                         strict=(fix or fix_promote or realign))
    if conn is None:
        return _views_result(EXIT_FAILURE)

    try:
        # ── W103: bracket list check ──────────────────────────────────────
        w103 = _check_w103_brackets(conn, archive_root)

        # ── W110/W119: Ahnentafel placement + direct-line stub checks ────
        try:
            fha_cfg = load_fha_yaml(archive_root, strict=True)
        except FhaConfigError as e:
            print(f'ERROR: {e}', file=sys.stderr)
            return _views_result(EXIT_FAILURE)
        root_person_raw = fha_cfg.get('root_person')
        w110: list[dict] = []
        w119: list[dict] = []
        w120: list[dict] = []
        w127: list[dict] = []

        if root_person_raw:
            root_pid = normalize_id(str(root_person_raw))
            if conn.execute('SELECT id FROM persons WHERE id=?', (root_pid,)).fetchone() is None:
                print(
                    f'WARNING: root_person {root_pid!r} is not in the index - '
                    'Ahnentafel checks (W110/W119) skipped. Run `fha index` or fix fha.yaml.',
                    file=sys.stderr,
                )
            else:
                try:
                    # root_generation (#72) must be 'self' (default) or
                    # 'children' - anything else is rejected here rather than
                    # silently read as 'self', mirroring the
                    # unresolvable-root_person case just above: a broken
                    # config value gets nothing guessed from it.
                    root_generation = resolve_root_generation(fha_cfg)
                except FhaConfigError as e:
                    print(
                        f'WARNING: {e} Ahnentafel checks (W110/W119/W127) skipped.',
                        file=sys.stderr,
                    )
                else:
                    root_position = root_generation_seed_position(root_generation)
                    # W127 before the walk, because it is about where the walk
                    # STARTS: everything below numbers correctly relative to a
                    # root that may itself be a generation too high (#70).
                    # Skipped entirely under root_generation: children (#72) -
                    # that field says a child on record is deliberate, so
                    # there is nothing accidental here for W127 to catch.
                    w127 = (_check_w127_root_anchor(conn, root_pid)
                            if root_generation != 'children' else [])
                    sex_gaps: list[dict] = []
                    pid_to_pos = _build_ahnentafel_map(
                        conn, root_pid, sex_gaps, root_position=root_position)
                    # W120: a slot the derivation DEFAULTED rather than derived - a
                    # lone linked parent with no recorded sex: takes the father
                    # (even) slot, and W110 can never catch a wrong outcome because
                    # the folders match their own flawed derivation. Report-only:
                    # the fix is a fact about a person only the human can record.
                    for gap in sex_gaps:
                        gap_name = _person_name_from_db(conn, gap['pid'])
                        row = conn.execute(
                            'SELECT path FROM persons WHERE id = ?',
                            (gap['pid'],)).fetchone()
                        where = (row['path'] or 'fha.yaml') if row else 'fha.yaml'
                        w120.append({
                            'code': 'W120',
                            'pid': gap['pid'],
                            'pos': gap['pos'],
                            'msg': (
                                f'W120 {where}: ' + format_w120_message(
                                    gap_name, gap['pos'], gap.get('sex'),
                                    '`fha views brackets` (or `--realign`)')
                            ),
                        })
                    w110 = _check_w110_ahnentafel(conn, archive_root, pid_to_pos)
                    try:
                        w119 = _check_w119_direct_line_stubs(
                            conn, archive_root, pid_to_pos, generations)
                    except AmbiguousCoupleFolderError as e:
                        # Two folders share one couple prefix (e.g. '002 Father' and
                        # '002 Mother'), so the direct-line destinations cannot be
                        # derived without guessing which folder is the couple's.
                        # Refuse the whole run rather than file records arbitrarily.
                        print(
                            f'ERROR: two couple folders share the prefix '
                            f'{e.prefix:03d} - {" and ".join(e.folders)}. Direct-line '
                            'stubs cannot be filed without guessing which one is the '
                            'couple\'s folder, so the Ahnentafel checks are refused. '
                            'Rename one so the numeric prefixes are unique (a '
                            'non-ancestral marriage takes a letter suffix, e.g. '
                            f'"{e.prefix:03d}b …"; SPEC §12.2), run `fha index`, then '
                            'retry.',
                            file=sys.stderr,
                        )
                        return _views_result(EXIT_FAILURE)
        else:
            print(
                'INFO: root_person not set in fha.yaml - '
                'Ahnentafel checks (W110/W119) skipped.'
            )

        # ── Compose renames that touch the same folder ────────────────────
        w103, w110, w103_suppressed = _compose_folder_renames(w103, w110)

        # W127 rides in all_issues so it is printed and moves the exit code,
        # but never in the apply lists: it is report-only, and its repair is a
        # line in fha.yaml that no tool may write on a guess.
        all_issues = w127 + w103 + w110 + w119 + w120
        issue_data = {'w103': len(w103), 'w110': len(w110),
                      'w119': len(w119), 'w120': len(w120), 'w127': len(w127)}

        # ── Report ────────────────────────────────────────────────────────
        for item in all_issues:
            print(item['msg'])

        if w103_suppressed:
            n = len(w103_suppressed)
            print(
                f'INFO: {n} W103 bracket rename(s) suppressed - bracket was computed'
                ' with misplaced occupants that W110 will move. Rerun brackets'
                ' after applying W110 fixes to pick up the correct bracket.'
            )

        if not all_issues:
            print('brackets: no issues found.')
            return _views_result(EXIT_CLEAN, data=issue_data)

        if not fix and not fix_promote and not realign and not dry_run:
            # Report-only mode: findings emitted above, exit with warnings
            return _views_result(EXIT_WARNINGS, data=issue_data)

        # ── --realign: both halves, one combined preview, one Apply? gate ──
        if realign:
            if not (w103 or w110 or w119):
                # Only report-only notes (W120/W127) remain - nothing mechanical
                # to realign; the note above already names the human's fix.
                print(
                    '\nNothing to realign - the promoted tree already matches '
                    'the derivation.'
                )
                return _views_result(EXIT_WARNINGS, data=issue_data)

            # Recompute every promotion destination against the folder tree
            # as it will stand AFTER the fix half runs. This is exactly the
            # invalidation that forces --fix and --fix-promote into two
            # passes - undone here by recomputing instead of refusing.
            rename_map: dict[Path, Path] = {}
            for item in w103:
                rename_map[item['folder']] = item['folder'].parent / item['new_name']
            created_folders: list[Path] = []
            for item in w110:
                if item['kind'] == 'folder_rename':
                    rename_map[item['old_folder']] = item['new_folder']
                elif item['kind'] == 'file_move' and item.get('expected_folder'):
                    created_folders.append(item['expected_folder'])
            rebased, dest_problem = _realign_promotion_dests(
                w119, rename_map, archive_root, created_folders)
            if dest_problem is not None:
                print(f'ERROR: {dest_problem}', file=sys.stderr)
                return _views_result(EXIT_FAILURE, data=issue_data)

            _print_bracket_preview(w103, w110, archive_root)
            if w119:
                _print_promote_preview(w119, archive_root)
                if rebased:
                    print(
                        '\n  (promotion destinations reflect the folder '
                        'renames above - a "create" step becomes a reuse once '
                        'the rename lands)'
                    )

            # Preflight before the gate, not after it: --realign is presented
            # as one whole-tree pass, so a destination that is already taken
            # must stop it here rather than leave the renames applied and every
            # promotion skipped.
            conflicts = _bracket_fix_conflicts(w103, w110, archive_root)
            if conflicts:
                _print_fix_conflicts(conflicts)
                return _views_result(EXIT_FAILURE, data=issue_data)

            if dry_run:
                print('\n(dry-run: no changes written)')
                return _views_result(EXIT_WARNINGS, data=issue_data)

            if not _confirm_apply(yes):
                print('Aborted - no changes written.')
                return _views_result(EXIT_WARNINGS, data=issue_data)

            failures = 0
            if w103 or w110:
                failures, aborted = _apply_bracket_fixes(w103, w110, archive_root)
                if aborted:
                    print(
                        f'\nNo changes written - {failures} problem(s) '
                        'blocked the plan (see stderr). Fix those, then '
                        're-run.',
                        file=sys.stderr,
                    )
                    return _views_result(EXIT_FAILURE, data=issue_data)

            promoted = 0
            skipped_promotions = 0
            if w119 and failures:
                # The promotion destinations were rebased onto the POST-rename
                # tree. If any rename or move failed (a folder open in
                # Explorer on Windows, say), promoting into those names would
                # create the never-renamed folder BESIDE the old one and split
                # the couple across two same-prefix folders - the exact state
                # the two-pass --fix/--fix-promote flow existed to prevent.
                # Skip the promotions; the human resolves the rename and
                # re-runs, and the second pass sees the real tree.
                skipped_promotions = len(w119)
                print(
                    f'\n{skipped_promotions} promotion(s) NOT applied: '
                    f'{failures} rename/move(s) failed above, and the '
                    'promotion destinations assumed those renames had landed. '
                    'Resolve the failures (a folder open in another program is '
                    'the usual cause), then re-run `fha views brackets '
                    '--realign`.',
                    file=sys.stderr,
                )
            elif w119:
                promoted, pfail = _apply_promotions(w119, archive_root)
                failures += pfail

            changed, cache_warning = _drop_index_cache(
                archive_root, conn, 'The realignment was applied')
            if cache_warning is not None:
                print(cache_warning, file=sys.stderr)
                return _views_result(
                    EXIT_WARNINGS,
                    data={**issue_data, 'index_stale': True,
                          'promoted': promoted, 'failures': failures})

            done_parts = []
            if w103 or w110:
                done_parts.append('renames/moves applied')
            if w119 and not skipped_promotions:
                done_parts.append(f'{promoted} of {len(w119)} stub(s) promoted')
            print(
                f'\nRealigned - {", ".join(done_parts)}. Run `fha index` to '
                'rebuild the index, then `fha views refresh` to regenerate '
                'the companion views.'
            )
            if failures:
                print(f'{failures} item(s) not applied - see stderr.')
                return _views_result(EXIT_WARNINGS, changed=changed,
                                     data={**issue_data, 'failures': failures,
                                           'promoted': promoted})
            return _views_result(EXIT_CLEAN, changed=changed,
                                 data={**issue_data, 'promoted': promoted})

        # ── --fix-promote: the W119 promotions under the Apply? gate ─────
        if fix_promote:
            if not w119:
                print('\nNo direct-line stubs to promote (W119 is clear).')
                return _views_result(
                    EXIT_WARNINGS if (w103 or w110 or w120 or w127) else EXIT_CLEAN,
                    data=issue_data)
            _print_promote_preview(w119, archive_root)
            if dry_run:
                print('\n(dry-run: no changes written)')
                return _views_result(EXIT_WARNINGS, data=issue_data)
            if not _confirm_apply(yes):
                print('Aborted - no changes written.')
                return _views_result(EXIT_WARNINGS, data=issue_data)

            applied, failures = _apply_promotions(w119, archive_root)
            changed: list[str] = []
            if applied:
                # A promoted record moved out of stubs/ - a path change the
                # mtime-based staleness check cannot see, so the cache goes.
                changed, cache_warning = _drop_index_cache(
                    archive_root, conn,
                    f'Promoted {applied} of {len(w119)} - those records moved '
                    'and are filed under their couple folders')
                if cache_warning is not None:
                    print(cache_warning, file=sys.stderr)
                    return _views_result(
                        EXIT_WARNINGS,
                        data={**issue_data, 'index_stale': True,
                              'promoted': applied, 'failures': failures})
                print(
                    f'\nPromoted {applied} of {len(w119)}. Run `fha index` to '
                    'rebuild the index, then `fha views refresh` to generate '
                    'the new curated people\'s timeline and draft-queue '
                    '(and refresh their ## Sources sections).'
                )
            if failures:
                print(f'{failures} promotion(s) not applied - see stderr.')
                return _views_result(EXIT_WARNINGS, changed=changed,
                                     data={**issue_data, 'failures': failures})
            return _views_result(
                EXIT_WARNINGS if (w103 or w110 or w120 or w127) else EXIT_CLEAN,
                changed=changed, data=issue_data)

        # ── --fix / --dry-run: the W103/W110 renames and moves ────────────
        if fix and not (w103 or w110):
            notes = []
            if w119:
                notes.append(
                    'the W119 stub promotions above are applied with '
                    '`fha views brackets --fix-promote` (previewed, then '
                    'confirmed)')
            if w120:
                notes.append(
                    'a W120 note is settled by recording sex: M or sex: F on '
                    'the named person, not by a fix')
            if w127:
                # Every note in this list has to be named, or the sentence ends
                # in a bare full stop - and on an archive whose only finding is
                # W127, that empty sentence would be the whole answer to
                # `--fix`, right after printing that the anchor is wrong.
                notes.append(
                    'the W127 anchor note is settled by editing root_person in '
                    'fha.yaml, then `fha index` and `fha views brackets '
                    '--realign`, not by a fix here')
            print('\nNothing here is applied by --fix - ' + '; '.join(notes) + '.')
            return _views_result(EXIT_WARNINGS, data=issue_data)

        # ── Preview ───────────────────────────────────────────────────────
        _print_bracket_preview(w103, w110, archive_root)

        # Same preflight as --realign, in the same place: the human is never
        # asked to confirm a plan that cannot be applied, and --dry-run reports
        # exactly what the apply would.
        conflicts = _bracket_fix_conflicts(w103, w110, archive_root)
        if conflicts:
            _print_fix_conflicts(conflicts)
            return _views_result(EXIT_FAILURE, data=issue_data)

        if dry_run:
            print('\n(dry-run: no changes written)')
            return _views_result(EXIT_WARNINGS, data=issue_data)

        # ── Confirm and apply ─────────────────────────────────────────────
        if not _confirm_apply(yes):
            print('Aborted - no changes written.')
            return _views_result(EXIT_WARNINGS, data=issue_data)

        failures, aborted = _apply_bracket_fixes(w103, w110, archive_root)
        if aborted:
            print(
                f'\nNo changes written - {failures} problem(s) blocked the '
                'plan (see stderr). Fix those, then re-run.',
                file=sys.stderr,
            )
            return _views_result(EXIT_FAILURE, data=issue_data)

        changed, cache_warning = _drop_index_cache(
            archive_root, conn, 'The renames and moves were applied')
        if cache_warning is not None:
            print(cache_warning, file=sys.stderr)
            data = {**issue_data, 'index_stale': True}
            if failures:
                data['failures'] = failures
            return _views_result(EXIT_WARNINGS, data=data)

        if failures:
            print(
                f'\nDone with {failures} item(s) not applied - see stderr.'
                ' Run `fha index` to rebuild the index with updated paths.'
            )
            return _views_result(EXIT_WARNINGS, changed=changed,
                                 data={**issue_data, 'failures': failures})
        print(
            '\nDone. Run `fha index` to rebuild the index with updated paths.'
        )
        return _views_result(EXIT_CLEAN, changed=changed, data=issue_data)

    finally:
        conn.close()


def _cmd_brackets(args: argparse.Namespace) -> int:
    """argparse → run_brackets bridge; returns the process exit code."""
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return run_brackets(
        archive_root,
        fix=getattr(args, 'fix', False),
        dry_run=getattr(args, 'dry_run', False),
        fix_promote=getattr(args, 'fix_promote', False),
        generations=getattr(args, 'generations', None),
        realign=getattr(args, 'realign', False),
        yes=getattr(args, 'yes', False),
    ).exit_code


# ── Tree view ─────────────────────────────────────────────────────────────────

def _build_nodes_bulk(conn: sqlite3.Connection, pids: list[str]) -> dict[str, dict]:
    """Build TOOLING §7 node dicts for all pids using two SQL queries instead of 2N.

    Returns an ordered dict keyed by lowercase pid, preserving the BFS encounter
    order of the input list.  Falls back gracefully for pids absent from the index.

    TOOLING §7 D3: vitals carry the date_edtf from the first accepted birth/death
    claim the person is the SUBJECT of - not merely one that names them
    (`_lib.claim_is_own_vital`, #126). A birth certificate names the baby, both
    parents and the informant; keyed on person_id alone this labelled the mother's
    node with her son's birth year, and a chart label is the shortest, most
    quotable fact a reader takes away. The site build asks the same question in
    `site._person_vitals`, through the same rule, so the two renderers of one
    tree cannot disagree. A claim naming at most one person with no `roles:`
    map at all has not said, and keeps the behaviour it always had (nobody
    else to be ambiguous about); a claim naming two or more people with no
    `roles:` map at all has not said which of them it is about, and
    `vital_subjects` scopes it to none of them (#126, reopened) - no node
    label picks up a date from it.

    A person often has several accepted claims for one vital, a certificate
    and a family memory say, and they can disagree. The most exact one labels
    the node (`_lib.vital_pick_key`, #201: narrowest date, then confidence,
    then claim id), the same pick the site makes, so the tree and the site
    never show different dates for one person.
    """
    if not pids:
        return {}
    placeholders = ','.join('?' * len(pids))

    person_map = {
        row['id']: row
        for row in conn.execute(
            f'SELECT id, name, sex FROM persons WHERE id IN ({placeholders})',
            pids,
        ).fetchall()
    }

    vitals_map: dict[str, dict] = {p: {'birth': None, 'death': None} for p in pids}
    subjects: dict[str, list[str] | None] = {}
    for row in sorted(conn.execute(
        f"""
        SELECT cp.person_id, c.id, c.type, c.date_edtf, c.date_min, c.date_max, c.confidence
        FROM claims c JOIN claim_persons cp ON c.id = cp.claim_id
        WHERE cp.person_id IN ({placeholders})
          AND c.type IN ('birth', 'death') AND c.status = 'accepted'
          AND COALESCE(c.negated, 0) = 0
        ORDER BY c.id
        """,
        pids,
    ).fetchall(), key=vital_pick_key):
        if not claim_is_own_vital(
                conn, row['person_id'], row['id'], row['type'], subjects):
            continue
        if vitals_map[row['person_id']][row['type']] is None:
            vitals_map[row['person_id']][row['type']] = row['date_edtf'] or None

    return {
        pid: {
            'p_id': fmt_id_display(pid),
            'name': person_map[pid]['name'] if pid in person_map else pid,
            'sex': person_map[pid]['sex'] if pid in person_map else None,
            'vitals': vitals_map[pid],
        }
        for pid in pids
    }


def _collect_edges(
    conn: sqlite3.Connection, pid: str, rels: list[str]
) -> list[dict]:
    """Fetch distinct outbound relationship edges from the relationships table.

    The index builder may insert duplicate rows when a claim has multiple
    claim_persons entries - DISTINCT prevents those duplicates from appearing
    as repeated edges in the traversal.

    Returns a list of internal edge dicts:
        {type, from, to, claim_id, date_start, date_end}
    IDs are lowercase as stored; callers apply fmt_id_display at output time.
    date_start / date_end are None when the table holds an empty string.
    """
    if not rels:
        return []
    placeholders = ','.join('?' * len(rels))
    rows = conn.execute(
        f"""
        SELECT DISTINCT r.rel, r.other_id, r.claim_id, r.date_start, r.date_end, c.subtype,
               EXISTS(SELECT 1 FROM relationships r2 WHERE r2.claim_id = r.claim_id
                      AND r2.rel IN ('parent', 'child')) AS records_parentage
        FROM relationships r LEFT JOIN claims c ON r.claim_id = c.id
        WHERE r.person_id = ? AND r.rel IN ({placeholders})
        """,
        [pid] + rels,
    ).fetchall()

    def nature(row) -> str | None:
        # On a claim that also records a parent tie, `subtype:` is that
        # tie's nature (SPEC §8.2): the marriage or friendship the same
        # claim records is not "adoptive".
        if row['rel'] not in ('parent', 'child') and row['records_parentage']:
            return None
        return (row['subtype'] or '').strip().lower() or None

    return [
        {
            'type': row['rel'],
            'from': pid,
            'to': row['other_id'],
            'claim_id': row['claim_id'],
            # The bond's nature (SPEC §12.2): a non-genetic parent/child edge draws
            # distinctly; unset/legacy subtypes default to genetic, back-compatibly.
            'subtype': nature(row),
            'genetic': is_genetic_parent_subtype(nature(row)),
            'date_start': row['date_start'] or None,
            'date_end': row['date_end'] or None,
        }
        for row in rows
    ]


def _traverse_tree(
    conn: sqlite3.Connection,
    seed_pid: str,
    mode: str,
    max_hops: int | None,
) -> tuple[dict[str, dict], dict[tuple, dict]]:
    """BFS traversal of the relationships graph from seed_pid.

    Collects P-ids and edges during BFS, then builds all node dicts in a
    single batch (two SQL queries via _build_nodes_bulk) rather than 2N queries.

    Returns:
        nodes - ordered dict pid → node dict (BFS encounter order)
        edges - ordered dict (from, to, type) → edge dict (deduplicated)

    Modes and traversal logic (TOOLING §7):
        'ancestors'   - expand via 'parent' edges only (pedigree chart).
        'descendants' - expand via 'child' edges.  At every visited node,
                        spouse edges are also collected and the spouse added
                        as a node, but spouses are NOT enqueued for further
                        expansion (one-hop only - pulls in in-laws without
                        recursing into their own ancestry).
        'fan'         - expand via all edge types (parent, child, spouse,
                        friend, associate, neighbor).  max_hops defaults to
                        2 at the call site.

    Cycle detection: the visited set is seeded with seed_pid before the loop
    starts.  Any P-id already in visited is never re-enqueued, so even a
    self-referential edge or cousin-marriage cycle terminates cleanly.
    """
    pids_in_order: list[str] = [seed_pid]
    pids_seen: set[str] = {seed_pid}
    edges: dict[tuple, dict] = {}
    visited: set[str] = {seed_pid}

    queue: deque[tuple[str, int]] = deque([(seed_pid, 0)])

    if mode == 'ancestors':
        traverse_rels: list[str] = ['parent']
        extra_rels: list[str] = []
    elif mode == 'descendants':
        traverse_rels = ['child']
        extra_rels = ['spouse']
    else:  # fan - all relationship types
        traverse_rels = ['parent', 'child', 'spouse', 'friend', 'associate', 'neighbor']
        extra_rels = []

    while queue:
        pid, depth = queue.popleft()

        # Expand BFS edges (hop-limit check gates further enqueuing, not edge collection)
        if max_hops is None or depth < max_hops:
            for edge in _collect_edges(conn, pid, traverse_rels):
                ekey = (edge['from'], edge['to'], edge['type'])
                if ekey not in edges:
                    edges[ekey] = edge
                other = edge['to']
                if other not in visited:
                    visited.add(other)
                    queue.append((other, depth + 1))
                if other not in pids_seen:
                    pids_in_order.append(other)
                    pids_seen.add(other)

        # Collect extra_rels as leaf nodes - never enqueued (in-law lineages
        # appear in the tree but their own ancestry is not followed).
        for edge in _collect_edges(conn, pid, extra_rels):
            ekey = (edge['from'], edge['to'], edge['type'])
            if ekey not in edges:
                edges[ekey] = edge
            other = edge['to']
            if other not in pids_seen:
                pids_in_order.append(other)
                pids_seen.add(other)

    nodes = _build_nodes_bulk(conn, pids_in_order)
    return nodes, edges


def _edge_to_json_dict(edge: dict) -> dict:
    """Convert an internal edge dict to the TOOLING §7 JSON edge schema.

    dates.start / dates.end are populated only for spouse edges; all other
    relationship types use null per TOOLING §7.  The claim_id and P-ids are
    written in the one all-lowercase ID form (SPEC §10).
    """
    is_spouse = edge['type'] == 'spouse'
    return {
        'type': edge['type'],
        'from': fmt_id_display(edge['from']),
        'to': fmt_id_display(edge['to']),
        'claim_id': fmt_id_display(edge['claim_id']) if edge['claim_id'] else None,
        'subtype': edge.get('subtype'),
        'genetic': edge.get('genetic', True),
        'dates': {
            'start': edge['date_start'] if is_spouse else None,
            'end': edge['date_end'] if is_spouse else None,
        },
    }


def _tree_to_json(
    seed_pid: str, mode: str, nodes: dict, edges: dict
) -> str:
    """Serialize the traversal result to the TOOLING §7 neutral JSON format.

    The JSON is the stable data contract between fha views tree and the site
    generator (TOOLING §7 D3).  The renderer adapter in fha site maps this
    shape to whatever tree library is vendored.
    """
    out = {
        'seed': fmt_id_display(seed_pid),
        'mode': mode,
        'nodes': list(nodes.values()),
        'edges': [_edge_to_json_dict(e) for e in edges.values()],
    }
    return json.dumps(out, indent=2, ensure_ascii=False)


def _tree_to_dot(nodes: dict, edges: dict) -> str:
    """Serialize the traversal result to GraphViz DOT format.

    Node label: "{name}\\n({birth}–{death})" where absent dates render as
    empty string (not 'None').  Edge labels are the relationship type string.
    The \\n in label strings is the DOT escape for a line break in the
    rendered graph, not a Python newline - hence the double backslash.
    """
    lines = ['digraph {', '  rankdir=TB;']

    for node in nodes.values():
        pid = node['p_id']
        name = (node['name'] or pid).replace('"', '\\"')
        birth = node['vitals'].get('birth') or ''
        death = node['vitals'].get('death') or ''
        label = f'{name}\\n({birth}–{death})'
        lines.append(f'  "{pid}" [label="{label}"];')

    for edge in edges.values():
        from_pid = nodes[edge['from']]['p_id']
        to_pid = nodes[edge['to']]['p_id']
        lines.append(f'  "{from_pid}" -> "{to_pid}" [label="{edge["type"]}"];')

    lines.append('}')
    return '\n'.join(lines) + '\n'


def run_tree(
    archive_root: Path,
    person_id: str | None,
    mode: str | None,
    generations: int | None,
    fmt: str,
    out_file: str | None,
) -> Result:
    """Build the relationship tree and return a Result (prints the report inline).

    Traverses the relationships table from the seed person using BFS and
    emits the result as neutral JSON (TOOLING §7 D3) or GraphViz DOT.
    `fmt='html'` is refused with a pointer to `fha site` until the site's
    full-tree feature lands (TOOLING §7 D6) - the HTML tree will be a thin
    single-file wrap of that renderer, never a stopgap built twice.

    Exit codes follow the §1 convention: 0 clean, 1 warnings, 3 tool failure.
    Missing index → exit 3 (tool cannot run without it).  The rendered tree text
    lands in `data['output']`; an `--out FILE` write is recorded in `changed`.
    """
    if not person_id:
        print('ERROR: a P-id argument is required.', file=sys.stderr)
        return _views_result(EXIT_FAILURE)

    if not mode:
        print('ERROR: --mode ancestors|descendants|fan is required.', file=sys.stderr)
        return _views_result(EXIT_FAILURE)

    # 'html' is accepted by argparse (so the refusal can explain itself in
    # plain language instead of a bare choices error) but refused here: the
    # HTML tree ships with the site's full-tree feature, as a thin wrap of
    # that renderer. The content views DO render html today (TOOLING §7 D11).
    if fmt == 'html':
        print(
            "ERROR: an HTML tree is not available from `fha views tree` yet - "
            "it arrives with the site's full family tree feature. For a "
            "browsable tree today run `fha site` (person pages include "
            "pedigree and descendant charts), or use `--format json` / "
            "`--format dot` for tree data.",
            file=sys.stderr,
        )
        return _views_result(EXIT_FAILURE)

    seed_pid = normalize_id(person_id)

    # Fan mode defaults to 2 hops; ancestors and descendants are unlimited
    # unless the caller supplies --generations.
    if generations is not None:
        max_hops = generations
    elif mode == 'fan':
        max_hops = 2
    else:
        max_hops = None

    conn = open_index_db(archive_root, ('persons',))
    if conn is None:
        return _views_result(EXIT_FAILURE)
    try:
        row = conn.execute('SELECT id FROM persons WHERE id = ?', (seed_pid,)).fetchone()
        if row is None:
            print(f'ERROR: person {seed_pid!r} not found in index.', file=sys.stderr)
            return _views_result(EXIT_WARNINGS)
        nodes, edges = _traverse_tree(conn, seed_pid, mode, max_hops)
    finally:
        conn.close()

    if fmt == 'dot':
        output = _tree_to_dot(nodes, edges)
    else:
        output = _tree_to_json(seed_pid, mode, nodes, edges)

    changed: list[str] = []
    if out_file:
        Path(out_file).write_text(output, encoding='utf-8')
        print(out_file)
        changed.append(str(out_file))
    else:
        print(output)

    return _views_result(EXIT_CLEAN, changed=changed,
                         data={'output': output, 'out_file': out_file})


def _cmd_tree(args: argparse.Namespace) -> int:
    """argparse → run_tree bridge; returns the process exit code."""
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return run_tree(
        archive_root,
        getattr(args, 'person_id', None),
        getattr(args, 'mode', None),
        getattr(args, 'generations', None),
        getattr(args, 'format', 'json') or 'json',
        getattr(args, 'out', None),
    ).exit_code


# ── CLI command handlers ──────────────────────────────────────────────────────

def _open_index_or_explain(archive_root: Path):
    """`open_index_db(strict=True)`, plus the cause it cannot know about.

    Views need a fresh index and refuse without one - correctly. But since a
    record folder that will not list holds the index at 'stale' by design
    (`_lib.newest_record_mtime` reports 'now' rather than a watermark it
    cannot stand behind), the shared refusal - "index is stale; run `fha
    index`" - becomes an instruction to loop: he runs it, `fha index` says the
    same thing, and the view never comes.

    `open_index_db` lives in `_lib` and speaks for every tool, so the extra
    sentence is added here, on the caller's side, where the folders can be
    named. Printed only when there really is an unreadable record folder, so
    the ordinary stale case reads exactly as it always did.
    """
    conn = open_index_db(archive_root, ('persons',), strict=True)
    if conn is not None:
        return conn
    unreadable: list[Path] = []
    on_error = unreadable_dir_recorder(unreadable)
    for name in ('sources', 'people', 'notes'):
        for _ in walk_files(archive_root / name, suffix='.md', on_error=on_error):
            pass
    if unreadable:
        shown = []
        for path in unreadable:
            try:
                shown.append(path.relative_to(archive_root).as_posix())
            except ValueError:
                shown.append(str(path).replace('\\', '/'))
        listed = ', '.join(sorted(shown)[:5])
        print(
            f'  Running `fha index` will not clear that on its own: '
            f'{len(unreadable)} folder(s) could not be opened, so the index '
            f'cannot be brought up to date over them ({listed}). This is '
            f'usually a folder whose permissions changed, or a drive or '
            f'network share that is not connected - reconnect it (or restore '
            f'your access to the folder), then run `fha index` and this '
            f'command again.',
            file=sys.stderr,
        )
    return None


def run_timeline(
    archive_root: Path,
    person_id: str | None = None,
    all_curated: bool = False,
    fmt: str = 'md',
) -> Result:
    """Generate timeline companion file(s); return a Result (prints progress inline).

    Generated files are recorded in `changed`; the per-file "timeline ->…" lines
    stay inline as before so output is byte-for-byte unchanged.

    `fmt='md'` (default) writes the companion file beside the profile exactly
    as always; `fmt='html'` writes the standalone single-file twin under
    generated/views/ - same query, same lines (D11).  An HTML write touches
    nothing the indexer scans, so it never stales the index: no reindex advice
    is printed and the run exits 0 clean.

    A `--all-curated` batch that skipped anyone (each skip prints its own
    WARNING and reason) exits 1 with `data['skipped']`, matching what the
    single-person path returns for the same condition - a partly written batch
    must never read as clean.
    """
    _VIEW_LINKS.clear()   # file names may have changed since the last run
    precheck = _format_precheck(fmt, ('md', 'html'))
    if precheck is not None:
        return precheck
    conn = _open_index_or_explain(archive_root)
    if conn is None:
        return _views_result(EXIT_FAILURE)

    changed: list[str] = []
    try:
        if all_curated:
            person_ids = _view_eligible_curated_ids(conn, archive_root)
            if not person_ids:
                return _empty_curated_views_result(conn)
            count = 0
            skipped = 0
            for pid in person_ids:
                out = _generate_or_warn(
                    f'timeline for {pid}', _generate_timeline, conn, pid, archive_root, fmt=fmt,
                )
                if out:
                    print(f'  timeline ->{out.relative_to(archive_root)}')
                    changed.append(str(out))
                    count += 1
                else:
                    skipped += 1
            print(f'Generated {count} timeline file(s).')
            sync = 'indexed'
            if count and fmt == 'md':
                # A companion write does NOT stale the index (#37: generated
                # views are excluded from the freshness watermark - they are
                # written FROM it), so the index's own rows for these files are
                # brought up to date here instead. Only when that cannot happen
                # is a rebuild worth advising.
                sync = _sync_written_views(
                    archive_root, conn, [Path(c) for c in changed], 'file(s)')
            return _batch_view_result(count, skipped, changed, 'timeline', sync)

        if not person_id:
            print('ERROR: provide a P-id or --all-curated.', file=sys.stderr)
            return _views_result(EXIT_FAILURE)

        pid = normalize_id(person_id)
        if _skip_stub_person(conn, pid, 'timeline', archive_root):
            return _views_result(EXIT_WARNINGS, data={'count': 0})
        out = _generate_timeline(conn, pid, archive_root, fmt=fmt)
        if out:
            print(f'  timeline ->{out.relative_to(archive_root)}')
            sync = ('indexed' if fmt != 'md' else
                    _sync_written_views(archive_root, conn, [out], 'file'))
            changed.append(str(out))
            return _single_view_result(sync, changed)
        return _views_result(EXIT_WARNINGS, data={'count': 0})

    except GeneratedFileRefused as e:
        return _views_result(_refused_exit(e))
    except GeneratedFileParentMissing as e:
        return _views_result(_missing_parent_exit(e))
    except OSError as e:
        return _views_result(_write_os_error_exit(e))
    except RuntimeError as e:
        return _views_result(_runtime_error_exit(e))
    finally:
        conn.close()


def _cmd_timeline(args: argparse.Namespace) -> int:
    """argparse → run_timeline bridge; returns the process exit code."""
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return run_timeline(
        archive_root,
        person_id=getattr(args, 'person_id', None),
        all_curated=getattr(args, 'all_curated', False),
        fmt=getattr(args, 'format', 'md') or 'md',
    ).exit_code


def run_sources_index(
    archive_root: Path,
    person_id: str | None = None,
    all_curated: bool = False,
    fmt: str = 'md',
) -> Result:
    """Refresh each named profile's own `## Sources` region; return a Result.

    Progress narration stays inline; written profiles land in `changed`.
    There is no separate sources-index file to write: a couple folder has no
    sources index of its own (SPEC 2.1 Part IV - its two profiles already
    list their sources), and `fmt='html'` is refused for the same reason.

    #76: the PER-PERSON form no longer writes a separate file - it rewrites
    the profile's own `## Sources` region in place (`_generate_sources_index_
    person`), so there is no HTML twin for it any more (refused below, naming
    the alternative) and its index row needs the DIFFERENT sync `_lib.
    resync_person_profile_rows` performs, not `_sync_written_views` (which is
    for the companion files #37 excludes from the freshness watermark - a
    profile was never excluded, so without this surgical sync the edit would
    correctly stale the index and break the very next `fha views` call in
    the same close-out sequence; see that function's own docstring). It also
    works on a STUB (no `_skip_stub_person` gate) - see the module docstring
    for why.

    A batch that skipped anything exits 1 with `data['skipped']` (see
    `_batch_view_result`).
    """
    _VIEW_LINKS.clear()   # file names may have changed since the last run
    precheck = _format_precheck(fmt, ('md', 'html'))
    if precheck is not None:
        return precheck
    if fmt == 'html':
        print(
            'ERROR: --format html has no sources list to build - each '
            "person's sources are listed in the ## Sources section of their "
            'own record (refresh it with `fha views sources-index <p-id>`, no '
            "--format flag). Use `fha site` to publish the person's page.",
            file=sys.stderr)
        return _views_result(EXIT_FAILURE)
    conn = _open_index_or_explain(archive_root)
    if conn is None:
        return _views_result(EXIT_FAILURE)

    changed: list[str] = []
    try:
        if all_curated:
            count = 0
            skipped = 0
            # Refresh every curated profile's own ## Sources region.
            # --all-curated stays curated-tier-only by its own name, matching
            # timeline/draft-queue's batch scope; a stub's region is refreshed
            # one at a time (fha views sources-index <P-id>).
            written: set[Path] = set()
            for pid in _view_eligible_curated_ids(conn, archive_root):
                out = _generate_or_warn(
                    f'sources-index for {pid}', _generate_sources_index_person,
                    conn, pid, archive_root, fmt=fmt, written=written,
                )
                if out:
                    print(f'  sources-index ->{out.relative_to(archive_root)}')
                    changed.append(str(out))
                    count += 1
                else:
                    skipped += 1

            print(f'Generated {count} sources-index file(s).')
            if count:
                # A refreshed PROFILE is not a companion excluded from the
                # freshness watermark (#37) - `resync_person_profile_rows`
                # keeps the write freshness-neutral, so the very next
                # `fha views` call in the same close-out sequence does not
                # refuse on "index is stale" (see that function's docstring).
                sync = resync_person_profile_rows(
                    archive_root, [Path(c) for c in changed if Path(c) in written])
                return _batch_view_result(
                    count, skipped, changed, 'sources-index', sync)
            if not skipped:
                # Nothing generated because every curated record is parked in
                # people/stubs/. Only when nothing was SKIPPED either: a run
                # where every eligible person failed has its own reason,
                # already printed, and must not be explained as a filing
                # problem.
                return _empty_curated_views_result(conn)
            return _batch_view_result(count, skipped, changed, 'sources-index')

        if not person_id:
            print('ERROR: provide a P-id or --all-curated.', file=sys.stderr)
            return _views_result(EXIT_FAILURE)

        # No _skip_stub_person gate here (unlike timeline/draft-queue): #76
        # wants a stub's ## Sources populated BEFORE promotion, not just after
        # - see the module docstring. A merged tombstone or unknown pid still
        # resolves to "no profile found" below, same as it always did.
        pid = normalize_id(person_id)
        out = _generate_sources_index_person(conn, pid, archive_root, fmt=fmt)
        if out:
            print(f'  sources-index ->{out.relative_to(archive_root)}')
            changed.append(str(out))
            sync = resync_person_profile_rows(archive_root, [out])
            return _single_view_result(sync, changed)
        return _views_result(EXIT_WARNINGS, data={'count': 0})

    except GeneratedFileRefused as e:
        return _views_result(_refused_exit(e))
    except GeneratedFileParentMissing as e:
        return _views_result(_missing_parent_exit(e))
    except OSError as e:
        return _views_result(_write_os_error_exit(e))
    except RuntimeError as e:
        return _views_result(_runtime_error_exit(e))
    finally:
        conn.close()


def _cmd_sources_index(args: argparse.Namespace) -> int:
    """argparse → run_sources_index bridge; returns the process exit code."""
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return run_sources_index(
        archive_root,
        person_id=getattr(args, 'person_id', None),
        all_curated=getattr(args, 'all_curated', False),
        fmt=getattr(args, 'format', 'md') or 'md',
    ).exit_code


def run_summary(
    archive_root: Path,
    person_id: str | None = None,
    all_curated: bool = False,
) -> Result:
    """Write each named profile's summary region (Born / Died / Married /
    Parents / Children, SPEC §16) in place; return a Result.

    Same shape as `run_sources_index`: the region lives inside the profile,
    so a single `<P-id>` works on a stub too, `--all-curated` is the curated
    batch, and every profile written is re-synced into the index so the next
    `fha views` call does not find it stale. A profile whose summary lines a
    human wrote by hand is left alone.
    """
    _VIEW_LINKS.clear()   # file names may have changed since the last run
    conn = _open_index_or_explain(archive_root)
    if conn is None:
        return _views_result(EXIT_FAILURE)
    changed: list[str] = []
    try:
        records = _summary_completeness_context(conn, archive_root)
        if all_curated:
            count = skipped = 0
            written: set[Path] = set()
            for pid in _view_eligible_curated_ids(conn, archive_root):
                out = _generate_or_warn(f'summary for {pid}', _generate_summary_person,
                                        conn, pid, archive_root, completeness=records,
                                        written=written)
                if out:
                    print(f'  summary       ->{out.relative_to(archive_root)}')
                    changed.append(str(out))
                    count += 1
                else:
                    skipped += 1
            print(f'Checked {count} summary region(s).')
            if count:
                sync = resync_person_profile_rows(
                    archive_root, [Path(c) for c in changed if Path(c) in written])
                return _batch_view_result(count, skipped, changed, 'summary', sync)
            if not skipped:
                return _empty_curated_views_result(conn)
            return _batch_view_result(count, skipped, changed, 'summary')

        if not person_id:
            print('ERROR: provide a P-id or --all-curated.', file=sys.stderr)
            return _views_result(EXIT_FAILURE)
        pid = normalize_id(person_id)
        out = _generate_summary_person(conn, pid, archive_root, completeness=records)
        if out:
            print(f'  summary       ->{out.relative_to(archive_root)}')
            changed.append(str(out))
            sync = resync_person_profile_rows(archive_root, [out])
            return _single_view_result(sync, changed)
        return _views_result(EXIT_WARNINGS, data={'count': 0})
    except OSError as e:
        return _views_result(_write_os_error_exit(e))
    except RuntimeError as e:
        return _views_result(_runtime_error_exit(e))
    finally:
        conn.close()


def _cmd_summary(args: argparse.Namespace) -> int:
    """argparse → run_summary bridge; returns the process exit code."""
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return run_summary(
        archive_root,
        person_id=getattr(args, 'person_id', None),
        all_curated=getattr(args, 'all_curated', False),
    ).exit_code


def run_draft_queue(
    archive_root: Path,
    person_id: str | None = None,
    all_curated: bool = False,
    fmt: str = 'md',
) -> Result:
    """Generate draft-queue companion file(s); return a Result (prints inline).

    Written files are recorded in `changed`; progress lines stay byte-identical.
    `fmt='html'` writes the standalone twin under generated/views/; HTML writes
    never stale the index, so the reindex advice stays md-only.

    A batch that skipped anything exits 1 with `data['skipped']` (see
    `_batch_view_result`).
    """
    _VIEW_LINKS.clear()   # file names may have changed since the last run
    precheck = _format_precheck(fmt, ('md', 'html'))
    if precheck is not None:
        return precheck
    conn = _open_index_or_explain(archive_root)
    if conn is None:
        return _views_result(EXIT_FAILURE)

    changed: list[str] = []
    try:
        if all_curated:
            person_ids = _view_eligible_curated_ids(conn, archive_root)
            if not person_ids:
                return _empty_curated_views_result(conn)
            count = 0
            skipped = 0
            for pid in person_ids:
                out = _generate_or_warn(
                    f'draft-queue for {pid}', _generate_draft_queue, conn, pid, archive_root, fmt=fmt,
                )
                if out:
                    print(f'  draft-queue ->{out.relative_to(archive_root)}')
                    changed.append(str(out))
                    count += 1
                else:
                    skipped += 1
            print(f'Generated {count} draft-queue file(s).')
            sync = 'indexed'
            if count and fmt == 'md':
                # A companion write does NOT stale the index (#37: generated
                # views are excluded from the freshness watermark - they are
                # written FROM it), so the index's own rows for these files are
                # brought up to date here instead. Only when that cannot happen
                # is a rebuild worth advising.
                sync = _sync_written_views(
                    archive_root, conn, [Path(c) for c in changed], 'file(s)')
            return _batch_view_result(count, skipped, changed, 'draft-queue', sync)

        if not person_id:
            print('ERROR: provide a P-id or --all-curated.', file=sys.stderr)
            return _views_result(EXIT_FAILURE)

        pid = normalize_id(person_id)
        if _skip_stub_person(conn, pid, 'draft-queue', archive_root):
            return _views_result(EXIT_WARNINGS, data={'count': 0})
        out = _generate_draft_queue(conn, pid, archive_root, fmt=fmt)
        if out:
            print(f'  draft-queue ->{out.relative_to(archive_root)}')
            sync = ('indexed' if fmt != 'md' else
                    _sync_written_views(archive_root, conn, [out], 'file'))
            changed.append(str(out))
            return _single_view_result(sync, changed)
        return _views_result(EXIT_WARNINGS, data={'count': 0})

    except GeneratedFileRefused as e:
        return _views_result(_refused_exit(e))
    except GeneratedFileParentMissing as e:
        return _views_result(_missing_parent_exit(e))
    except OSError as e:
        return _views_result(_write_os_error_exit(e))
    except RuntimeError as e:
        return _views_result(_runtime_error_exit(e))
    finally:
        conn.close()


def _cmd_draft_queue(args: argparse.Namespace) -> int:
    """argparse → run_draft_queue bridge; returns the process exit code."""
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return run_draft_queue(
        archive_root,
        person_id=getattr(args, 'person_id', None),
        all_curated=getattr(args, 'all_curated', False),
        fmt=getattr(args, 'format', 'md') or 'md',
    ).exit_code


# ── Place notes (SPEC §15) ────────────────────────────────────────────────────
# In Obsidian link mode every registered place gets one generated note,
# places/{slug}_{l-id}.md, so a place link has a file Obsidian can follow and
# the graph has one node per place. The note mirrors places.yaml (read here
# through the index, like every view); places.yaml stays the only truth. It
# opens with frontmatter (`aliases:`, which Obsidian reads only at the top of
# a file, and `location:` for a place with coordinates, which the map views
# read), then the GENERATED header, so its ownership is judged after the
# frontmatter (`after_frontmatter=True`). It lists no people or sources:
# Obsidian's backlinks pane shows those, and a list here would change on
# every refresh.


def _registered_places(conn: sqlite3.Connection) -> list[dict] | None:
    """Every registered place from the index, with its alt names and dated
    history in places.yaml order; None when the index has no place tables
    (built by an older tool: `fha index` adds them)."""
    try:
        rows = conn.execute(
            'SELECT id, name, hierarchy, within, lat, lon, notes FROM places ORDER BY id'
        ).fetchall()
        alts: dict[str, list[str]] = {}
        for r in conn.execute('SELECT place_id, alt_name FROM place_names ORDER BY rowid'):
            alts.setdefault(r['place_id'], []).append(str(r['alt_name']))
        history: dict[str, list[tuple]] = {}
        for r in conn.execute(
                'SELECT place_id, period_edtf, hierarchy FROM place_history ORDER BY rowid'):
            history.setdefault(r['place_id'], []).append((r['period_edtf'], r['hierarchy']))
    except sqlite3.Error:
        return None
    places = []
    for r in rows:
        name = ' '.join(str(r['name']).split()) if r['name'] else ''
        places.append({
            'id': normalize_id(r['id']), 'name': name or None,
            'hierarchy': r['hierarchy'], 'within': r['within'],
            'lat': r['lat'], 'lon': r['lon'], 'notes': r['notes'],
            'alt_names': alts.get(r['id'], []), 'history': history.get(r['id'], []),
        })
    return places


def _place_note_text(place: dict, names: dict[str, str]) -> str:
    """One place note, in the ID link form (the caller converts it).

    Frontmatter `aliases:` carries the place's ID, name and alt names, so
    Obsidian suggests the note when any of them is typed after `[[`, and a
    place with coordinates gets `location:` there, which Obsidian's map views
    read, so it shows on a map. The body
    is what places.yaml says: the hierarchy, the place it sits inside, the
    coordinates, the other names, the dated history and the notes. A field
    the registry leaves out is left out here too."""
    lid = place['id']
    shown = fmt_id_display(lid)
    title = place['name'] or shown
    aliases: list[str] = []
    for a in [place['name'], *place['alt_names']]:
        a = ' '.join(str(a or '').split())
        if a and a not in aliases and a.lower() != shown:
            aliases.append(a)
    out = ['---\n', 'aliases:\n', f'  - {shown}\n']
    out += [f'  - {json.dumps(a, ensure_ascii=False)}\n' for a in aliases]
    # Obsidian's map views (the Maps view of Bases, the Map View plugin) read
    # `location:`, so a mapped place shows on a map. Same numbers as the
    # Coordinates line below, so the two never disagree.
    if place['lat'] is not None and place['lon'] is not None:
        out.append(f'location: [{place["lat"]}, {place["lon"]}]\n')
    out += ['---\n', _gen_header('places'), f'# {title}\n', _purpose_block_md('places')]

    facts: list[str] = []
    if place['hierarchy']:
        facts.append(f'- **Place:** {place["hierarchy"]}')
    if place['within']:
        within = normalize_id(strip_link_wrapper(str(place['within'])))
        if within in names:
            facts.append(f'- **Inside:** [[{fmt_id_display(within)}|{names[within]}]]')
        else:
            facts.append(f'- **Inside:** {place["within"]}')
    if place['lat'] is not None and place['lon'] is not None:
        facts.append(f'- **Coordinates:** {place["lat"]}, {place["lon"]}')
    if place['alt_names']:
        facts.append(f'- **Also called:** {", ".join(place["alt_names"])}')
    if facts:
        out.append('\n' + '\n'.join(facts) + '\n')
    if place['history']:
        out.append('\n## History\n\n')
        for period, hierarchy in place['history']:
            when = humanize_edtf(period) if period else ''
            what = hierarchy or ''
            out.append(f'- {when}: {what}\n' if when and what else f'- {when or what}\n')
    notes = str(place['notes'] or '').strip()
    if notes:
        out.append(f'\n## Notes\n\n{notes}\n')
    return ''.join(out)


def _owned_place_note(path: Path) -> bool:
    """True when `path` is a place note `fha views places` wrote: its first
    line after the frontmatter is the places GENERATED marker. The same test
    the link writer uses (`_lib.is_owned_place_note`), so the two never
    disagree about which file is a place's note."""
    return is_owned_place_note(path)


def _place_record_files(archive_root: Path) -> list[Path]:
    """The record files whose place links follow a rename: every .md under
    people/, sources/ and notes/ that is not a template or a generated file
    (a generated file is rebuilt, never edited)."""
    files: list[Path] = []
    for sub in ('people', 'sources', 'notes'):
        base = archive_root / sub
        if base.is_dir():
            files += [p for p in sorted(walk_files(base, suffix='.md'))
                      if not is_template_file(p) and not is_generated_file(p)]
    return files


def _place_notes_pass(conn: sqlite3.Connection, archive_root: Path,
                      dry_run: bool = False) -> dict | None:
    """Write every place's note, remove the notes no place owns any more,
    and re-point file-name links left behind by a rename. Prints a line per
    change; returns the tallies, or None when the index has no place tables.

    The steps run in this order, each one safe to repeat:

      1. A generated note whose name no longer matches its place (the place
         was renamed in places.yaml) or whose place is gone is removed. A
         hand-written file in places/ is never touched: it is named and
         left.
      2. Each place's note is written. A note whose only change would be the
         date in its header is left alone, so a refresh on a new day
         rewrites nothing.
      3. Every link written with a place note's file name that is no longer
         current (`[[old-name_l-…|Fairview]]`) is re-pointed at the current
         one, in people/, sources/ and notes/. ID-form links are left for
         `fha normalize-links --obsidian` and lint W161. Because this looks
         for every stale name rather than only this run's renames, a run
         that stopped partway finishes the job the next time.
    """
    places = _registered_places(conn)
    if places is None:
        return None
    planned = {p['id']: place_note_stem(p['id'], p['name']) for p in places}
    names = {p['id']: p['name'] for p in places if p['name']}
    links = LinkWriter(archive_root, 'obsidian', place_stems=planned, place_names=names)
    places_dir = archive_root / 'places'
    stats = {'written': [], 'unchanged': 0, 'removed': [], 'skipped': 0,
             'relinked': [], 'links': 0}
    noted: dict[str, str] = {}      # l-id -> stem, for each place whose note is in place
    would = 'would ' if dry_run else ''

    existing: list[Path] = []
    if places_dir.is_dir():
        try:
            existing = sorted(p for p in places_dir.iterdir()
                              if p.is_file() and is_place_note_path(p, archive_root))
        except OSError as e:
            print(f'WARNING: could not list places/ ({e.strerror or e}); old place notes '
                  'were left as they are.', file=sys.stderr)
            stats['skipped'] += 1
    for path in existing:
        lid = path.stem.rsplit('_', 1)[-1].lower()
        if planned.get(lid) == path.stem:
            continue
        rel = _rel_display(path, archive_root)
        if not _owned_place_note(path):
            # The owner's own file (research notes on the town, say). It is
            # never a link target and never touched, so nothing is wrong:
            # say what it is once, and carry on.
            print(f'  note: {rel} carries place {fmt_id_display(lid)} in its name but is '
                  'not its note, so it was left as it is and no link points at it.')
            continue
        why = (f'the place is now called {names.get(lid) or fmt_id_display(lid)}'
               if lid in planned else 'that place is no longer in places.yaml')
        if not dry_run:
            try:
                path.unlink()
            except OSError as e:
                print(f'WARNING: could not remove {rel} ({e.strerror or e}); it may be '
                      'open in another program. Close it and run `fha views places` again.',
                      file=sys.stderr)
                stats['skipped'] += 1
                continue
        print(f'  {would}remove {rel} ({why})')
        stats['removed'].append(path)

    for place in places:
        path = places_dir / f'{planned[place["id"]]}.md'
        content = rewrite_record_links_to_stems(_place_note_text(place, names), links)[0]
        rel = _rel_display(path, archive_root)
        if path.exists():
            if not _owned_place_note(path):
                print(f'WARNING: skipped the note for {fmt_id_display(place["id"])}: '
                      f'{rel} has no GENERATED header, so it is yours. Move or delete '
                      'it, then run `fha views places` again.', file=sys.stderr)
                stats['skipped'] += 1
                continue
            try:
                same = not generated_text_differs(read_text_exact(path), content)
            except (OSError, UnicodeDecodeError):
                same = False
            if same:
                stats['unchanged'] += 1
                noted[place['id']] = planned[place['id']]
                continue
        if not dry_run:
            try:
                write_generated_file(path, content, _GEN_MARKER, create_parents=True,
                                     after_frontmatter=True)
            except (GeneratedFileRefused, OSError) as e:
                print(f'WARNING: skipped {rel} ({getattr(e, "strerror", None) or e}).',
                      file=sys.stderr)
                stats['skipped'] += 1
                continue
        print(f'  {would}write place note -> {rel}')
        stats['written'].append(path)
        noted[place['id']] = planned[place['id']]

    # Only links to a note that is there (or, in a dry run, would be) move:
    # a link re-pointed at a note that failed to write would lead nowhere.
    for path in _place_record_files(archive_root) if noted else []:
        try:
            text = read_text_exact(path)
        except (OSError, UnicodeDecodeError):
            continue    # lint names a record it cannot read
        new, count = rewrite_stale_stem_links(text, noted)
        if not count:
            continue
        rel = _rel_display(path, archive_root)
        if not dry_run:
            try:
                write_text_exact_atomic(path, new)
            except OSError as e:
                print(f'WARNING: could not update the place links in {rel} '
                      f'({e.strerror or e}). Run `fha views places` again.', file=sys.stderr)
                stats['skipped'] += 1
                continue
        print(f'  {would}re-point {count} place link(s) in {rel}')
        stats['relinked'].append(path)
        stats['links'] += count
    return stats


def _place_notes_summary(stats: dict, dry_run: bool) -> None:
    """The closing lines of a place-notes pass."""
    verb = 'Would write' if dry_run else 'Wrote'
    print(f'{verb} {len(stats["written"])} place note(s); {stats["unchanged"]} already '
          'up to date.')
    if stats['relinked'] and not dry_run:
        print(f'Re-pointed {stats["links"]} place link(s) in {len(stats["relinked"])} '
              'record(s), so the index is now out of date. Run `fha index` before the '
              'next `fha views` or `fha find`.')
    if stats['skipped']:
        print(f'{stats["skipped"]} item(s) skipped; see the warnings above.')


_OBSIDIAN_BASES = ('People.base', 'Sources.base', 'Places.base')
_OBSIDIAN_BASES_DIR = Path('notes') / 'obsidian'


def _write_obsidian_bases(archive_root: Path, dry_run: bool = False) -> list[Path]:
    """Give an Obsidian owner ready-made tables of people, sources and places.

    Opening `notes/obsidian/People.base` in Obsidian shows everyone as a
    sortable table (curated people, stubs, people still marked living or
    unknown), with no plugin. They are written once, only with
    `links: obsidian`, and only when `notes/obsidian/` does not exist yet:
    Obsidian saves the owner's column and sort changes into the file, and a
    file the owner deleted stays deleted, so nothing here is ever rewritten
    or removed. The folder is outside people/, sources/ and places/, since a
    table owns no data (SPEC §3). Returns the files written (or that would be).
    """
    folder = archive_root / _OBSIDIAN_BASES_DIR
    if folder.exists():
        return []
    src_dir = Path(__file__).resolve().parent / 'templates' / 'obsidian'
    written: list[Path] = []
    for name in _OBSIDIAN_BASES:
        src = src_dir / name
        if not src.is_file():
            continue
        dest = folder / name
        written.append(dest)
        if dry_run:
            continue
        try:
            folder.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
        except OSError as e:
            print(f'WARNING: could not write {_OBSIDIAN_BASES_DIR.as_posix()}/{name} '
                  f'({e.strerror or e}); the Obsidian tables were skipped.', file=sys.stderr)
            return []
    if written:
        verb = 'would write' if dry_run else 'wrote'
        print(f'{verb} the Obsidian tables in {_OBSIDIAN_BASES_DIR.as_posix()}/ '
              f'({", ".join(_OBSIDIAN_BASES)}).')
    return written


def run_places(archive_root: Path, dry_run: bool = False) -> Result:
    """`fha views places`: the place notes for Obsidian link mode (SPEC §15).

    With `links: obsidian` in fha.yaml, writes one generated note per
    registered place in places/, removes the notes a rename or a removed
    place left behind, and re-points file-name links to a renamed place's
    note (`_place_notes_pass`). In `id` mode it writes nothing at all: there
    the tools write place links as IDs, so a note would have no links to
    receive. `--dry-run` prints the same lines and writes nothing.

    Exit codes: 0 done (or nothing to do), 1 something was skipped (each
    skip printed its reason and fix), 3 no usable index."""
    _VIEW_LINKS.clear()
    dry_run = bool(dry_run)
    if link_mode(load_fha_yaml(archive_root)) != 'obsidian':
        print('Place notes are written only when fha.yaml says `links: obsidian`; '
              'nothing was written.')
        leftover = [p for p in ((archive_root / 'places').glob('*.md')
                                if (archive_root / 'places').is_dir() else [])
                    if is_place_note_path(p, archive_root) and _owned_place_note(p)]
        if leftover:
            print(f'{len(leftover)} place note(s) from Obsidian mode are still in places/. '
                  '`fha views clean` removes them.')
        return _views_result(EXIT_CLEAN, data={'count': 0, 'mode': 'id'})
    _write_obsidian_bases(archive_root, dry_run=dry_run)
    conn = _open_index_or_explain(archive_root)
    if conn is None:
        return _views_result(EXIT_FAILURE)
    try:
        stats = _place_notes_pass(conn, archive_root, dry_run=dry_run)
    finally:
        conn.close()
    if stats is None:
        print('ERROR: the index has no place tables. Run `fha index`, then '
              '`fha views places` again.', file=sys.stderr)
        return _views_result(EXIT_FAILURE)
    _place_notes_summary(stats, dry_run)
    data = {'count': len(stats['written']), 'unchanged': stats['unchanged'],
            'removed': len(stats['removed']), 'links': stats['links'],
            'relinked': len(stats['relinked'])}
    if stats['skipped']:
        data['skipped'] = stats['skipped']
    # A re-pointed link is an edit to a record the index read, and only a
    # rebuild re-reads everything a record feeds (its search text, its
    # citation and title columns, a question's text), so the run says the
    # index is stale rather than leave it looking current.
    stale = bool(stats['relinked']) and not dry_run
    if stale:
        data['index_stale'] = True
    changed = [] if dry_run else [str(p) for p in
                                  stats['written'] + stats['removed'] + stats['relinked']]
    code = EXIT_WARNINGS if stats['skipped'] or stale else EXIT_CLEAN
    return _views_result(code, changed=changed, data=data)


def _cmd_places(args: argparse.Namespace) -> int:
    """argparse → run_places bridge; returns the process exit code."""
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return run_places(archive_root, dry_run=getattr(args, 'dry_run', False)).exit_code


def _unreadable_summary(count: int, dirs: int = 0) -> str:
    """The one closing line for files (and folders) `clean` could not read.

    A tally beside the "Removed N" line, because the per-file ERRORs go to
    stderr and a human reading only stdout would otherwise see a sweep that
    looks complete. A folder that would not open is counted separately: it
    hides an unknown number of files rather than one known file, so the two
    cannot honestly be added together.
    """
    text = (f'{count} file(s) could not be read, so they were left alone - '
            'see above.')
    if dirs:
        text += (f' {dirs} folder(s) could not be opened either, so anything '
                 'generated inside them was left as well.')
    return text


def run_clean(archive_root: Path, dry_run: bool = False) -> Result:
    """Delete GENERATED view files; return a Result (prints progress inline).

    Three sweeps, one ownership test (the views marker on the first non-blank
    line - marker-per-file, never folder ownership):
      1. companion `.md` files in the people/ tree, as always;
      2. single-file `.html` views (and any stray `.md`) under generated/views/;
      3. place notes in places/ (`{slug}_{l-id}.md`, Obsidian link mode),
         whose marker is the first line after their frontmatter. They carry
         no index rows, so nothing else needs cleaning up.
    Hand-authored files are never touched in either place.  Removed paths are
    recorded in `changed`; under --dry-run nothing is deleted, both sweeps are
    listed, and `changed` stays empty.

    TWO OWNERSHIP MODELS SINCE #76, ONE SWEEP. A file is either WHOLLY owned
    (a companion: first line is the marker, the whole file is disposable) or
    PARTIALLY owned (a profile carrying a `## Sources` GENERATED-BEGIN/END
    region: one section is machine-derived, the rest is the human's). This
    function only ever deletes the first kind - `_owned_by_views` tests a
    FILE's first non-blank line, and a profile's first line is always its
    frontmatter fence, never a region marker, so a profile is never even a
    candidate here no matter what it contains further down. Nothing below
    special-cases this; it falls out of the existing test by construction -
    see `tests/test_views_html.py::CleanSweepTests::
    test_clean_sweeps_generated_views_marker_per_file` for the regression
    proof (a profile with a region survives a sweep that still removes a
    companion, byte-for-byte, not just by filename).

    Exit codes: 0 clean. A people/-tree companion carries index rows
    (person_files, and its body in notes_fts), and those are deleted in the
    same pass - the freshness watermark never sees a generated companion (#37),
    so leaving them would keep `fha find --text` serving text whose file is
    gone. Only when that row cleanup fails, or when a file could not be read
    or deleted at all (locked, read-only), does the run exit 1 saying which.
    A file that will not read or delete is named and skipped, never allowed to
    abort the sweep.
    generated/views/ files are never indexed and need no cleanup at all.
    """
    dry_run = bool(dry_run)

    people_dir = archive_root / 'people'
    if not people_dir.is_dir():
        print('ERROR: people/ directory not found.', file=sys.stderr)
        return _views_result(EXIT_FAILURE)

    # Files whose first line could not be read at all. Ownership is decided by
    # that line, so an unreadable file is not "not ours" - it is unknown, and
    # dropping it silently made a partial sweep report as a complete one. Named
    # and counted instead, exactly like a file that will not delete.
    unreadable: list[Path] = []

    def _owned_by_views(p: Path) -> bool:
        """True when p carries the views GENERATED marker on its first non-blank
        line - marker-per-file, so a hand-written file that merely mentions the
        marker later is never deleted, and another tool's GENERATED file is not
        ours to remove either. An unreadable file is recorded and excluded."""
        try:
            text = p.read_text(encoding='utf-8', errors='ignore')
        except OSError as e:
            print(
                f'  ERROR could not read {_rel_display(p, archive_root)} '
                f'({e.strerror or e}) - it may be open in another program. '
                'Close it and re-run; it was left alone.',
                file=sys.stderr,
            )
            unreadable.append(p)
            return False
        return is_generated_text(text, prefix=_GEN_MARKER)

    # Walked with an error seam, not rglob: `clean`'s closing line is a count
    # of what it swept, and a folder that will not list makes a sweep that
    # missed a whole subtree read exactly like a complete one - the same
    # reasoning that already names files it could not read, one level up.
    unreadable_dirs: list[Path] = []
    found = [p for p in sorted(walk_files(
                 people_dir, suffix='.md',
                 on_error=unreadable_dir_recorder(unreadable_dirs)))
             if _owned_by_views(p)]
    for d in unreadable_dirs:
        print(
            f'  ERROR could not open {_rel_display(d, archive_root)} - any '
            f'generated view files inside it were left alone. Reconnect the '
            f'drive (or restore your access to the folder), then re-run '
            f'`fha views clean`.',
            file=sys.stderr,
        )

    # Second sweep: standalone HTML views under generated/views/ (D11). Same
    # marker-per-file ownership - the folder is visible and a human may park
    # anything there; only files views wrote are removed.
    gen_found: list[Path] = []
    gen_dir = archive_root / 'generated' / 'views'
    if gen_dir.is_dir():
        gen_found = [
            p for p in sorted(list(gen_dir.glob('*.html')) + list(gen_dir.glob('*.md')))
            if _owned_by_views(p)
        ]

    # Third sweep: the place notes Obsidian link mode writes in places/
    # (SPEC §15). Their marker follows the frontmatter, so ownership is
    # judged there; places.yaml and any hand-written file stay.
    place_found: list[Path] = []
    places_dir = archive_root / 'places'
    if places_dir.is_dir():
        for p in sorted(places_dir.glob('*.md')):
            if not is_place_note_path(p, archive_root):
                continue
            try:
                text = p.read_text(encoding='utf-8', errors='ignore')
            except OSError as e:
                print(f'  ERROR could not read {_rel_display(p, archive_root)} '
                      f'({e.strerror or e}); it may be open in another program. '
                      'Close it and re-run; it was left alone.', file=sys.stderr)
                unreadable.append(p)
                continue
            if is_generated_text(text, prefix=_GEN_MARKER, after_frontmatter=True):
                place_found.append(p)

    all_found = found + gen_found + place_found
    # A file it could not classify and a folder it could not open both mean
    # the same thing about the closing count: this sweep is not the complete
    # one it would otherwise claim to be.
    incomplete = bool(unreadable or unreadable_dirs)
    if not all_found:
        print('No GENERATED view files found.')
        if incomplete:
            print(_unreadable_summary(len(unreadable), len(unreadable_dirs)))
            return _views_result(EXIT_WARNINGS,
                                 data={'removed': 0, 'unreadable': len(unreadable),
                                       'unreadable_dirs': len(unreadable_dirs)})
        return _views_result(EXIT_CLEAN)

    changed: list[str] = []
    removed_paths: list[Path] = []
    failures = 0
    for p in all_found:
        rel = p.relative_to(archive_root)
        if dry_run:
            print(f'  would remove {rel}')
            continue
        try:
            p.unlink()
        except OSError as e:
            # A locked or read-only file (open in another program is the usual
            # cause on Windows). Name it and carry on with the rest of the
            # sweep - one stuck file must not abort the others, and the raw
            # OSError must never reach the human as a traceback.
            print(
                f'  ERROR could not remove {rel} ({e.strerror or e}) - it may '
                'be open in another program. Close it and re-run.',
                file=sys.stderr,
            )
            failures += 1
            continue
        print(f'  removed {rel}')
        removed_paths.append(p)
        changed.append(str(p))

    verb = 'Would remove' if dry_run else 'Removed'
    print(f'{verb} {len(all_found) if dry_run else len(removed_paths)} generated file(s).')
    if incomplete:
        # Printed for the dry run too: a preview that hides a file it could not
        # even classify is not the preview of the run it claims to be.
        print(_unreadable_summary(len(unreadable), len(unreadable_dirs)))
    if not dry_run:
        data = {'removed': len(removed_paths)}
        if failures:
            data['failures'] = failures
        if unreadable:
            data['unreadable'] = len(unreadable)
        if unreadable_dirs:
            data['unreadable_dirs'] = len(unreadable_dirs)
        people_companions = set(found)
        people_removed = [p for p in removed_paths if p in people_companions]
        if people_removed:
            # A deleted companion is invisible to the freshness watermark (#37
            # excludes generated views from it), so its rows would stay in
            # notes_fts and person_files - authoritative and wrong - until some
            # unrelated edit forced a rebuild. Drop them here instead.
            if _sync_index_rows(archive_root, removed=people_removed) == 'index_error':
                print('Note: deleted files still appear in .cache/index.sqlite - run `fha index` to update the cache.')
                data['index_stale'] = True
                return _views_result(EXIT_WARNINGS, changed=changed, data=data)
        if failures:
            print(f'{failures} file(s) could not be removed - see above.')
            return _views_result(EXIT_WARNINGS, changed=changed, data=data)
        if incomplete:
            return _views_result(EXIT_WARNINGS, changed=changed, data=data)
        return _views_result(EXIT_CLEAN, changed=changed, data=data)
    if incomplete:
        return _views_result(
            EXIT_WARNINGS,
            data={'removed': 0, 'would_remove': len(all_found),
                  'unreadable': len(unreadable),
                  'unreadable_dirs': len(unreadable_dirs)})
    return _views_result(EXIT_CLEAN, data={'removed': 0, 'would_remove': len(all_found)})


def _cmd_clean(args: argparse.Namespace) -> int:
    """argparse → run_clean bridge; returns the process exit code."""
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return run_clean(archive_root, dry_run=getattr(args, 'dry_run', False)).exit_code


def run_refresh(archive_root: Path, fmt: str = 'md') -> Result:
    """Regenerate every curated person's view files, and every person's two
    in-profile regions (## Sources, the summary lines - stubs included);
    return a Result (prints inline).

    Written files are recorded in `changed`; the per-file progress lines stay
    byte-for-byte as before.

    `fmt` selects the output set: 'md' (default) the companion files as
    always; 'html' the standalone generated/views/ set only; 'both' the two
    sets in one pass (owner decision 7-Q2).  The reindex advice prints only
    when .md companions were written, and only when their index rows could not
    be updated in place - HTML never stales the index either way. Per-person
    sources-index (#76) is a partial exception: it is refreshed once per
    person regardless of `fmt` (there is no html twin for it any more - it
    rewrites the profile's own `## Sources` region), and its index row is
    kept current by the DIFFERENT surgical sync `_lib.resync_person_profile_
    rows` performs (a profile was never excluded from the freshness
    watermark the way a companion is, so without it this edit would stale
    the index on its own).

    A refresh that skipped anything exits 1 with `data['skipped']` (see
    `_batch_view_result`): regenerating every view is the point of the verb, so
    a partial pass must not report success.
    """
    _VIEW_LINKS.clear()   # file names may have changed since the last run
    precheck = _format_precheck(fmt, ('md', 'html', 'both'))
    if precheck is not None:
        return precheck
    fmt_passes: tuple[str, ...] = ('md', 'html') if fmt == 'both' else (fmt,)
    if 'md' in fmt_passes and link_mode(load_fha_yaml(archive_root)) == 'obsidian':
        _write_obsidian_bases(archive_root)
    conn = _open_index_or_explain(archive_root)
    if conn is None:
        return _views_result(EXIT_FAILURE)

    changed: list[str] = []
    try:
        person_ids = _view_eligible_curated_ids(conn, archive_root)
        # Every other live person record - stubs, connections, a curated
        # record still under people/stubs/ - gets no companion files, but its
        # profile's own two generated regions (## Sources, the summary lines)
        # are kept current like any profile's: a stub reads the same way a
        # curated record does, just with less filled in (SPEC §16).
        region_only_ids = [
            r['id'] for r in conn.execute(
                "SELECT id FROM persons WHERE COALESCE(merged_into, '') = '' "
                "ORDER BY id").fetchall()
            if r['id'] not in set(person_ids)]
        if not person_ids and not region_only_ids:
            return _empty_curated_views_result(conn)

        # Obsidian link mode: the place notes first (SPEC §15), so every
        # view written below links places by their notes' current names.
        place_changed: list[str] = []
        place_skipped = 0
        place_relinked = False
        if 'md' in fmt_passes and link_mode(load_fha_yaml(archive_root)) == 'obsidian':
            stats = _place_notes_pass(conn, archive_root)
            if stats is None:
                print('WARNING: the index has no place tables, so no place notes were '
                      'written. Run `fha index`, then `fha views refresh` again.',
                      file=sys.stderr)
                place_skipped = 1
            else:
                _place_notes_summary(stats, dry_run=False)
                place_changed = [str(p) for p in
                                 stats['written'] + stats['removed'] + stats['relinked']]
                place_skipped = stats['skipped']
                place_relinked = bool(stats['relinked'])
            _VIEW_LINKS.clear()

        records = _summary_completeness_context(conn, archive_root) if 'md' in fmt_passes else None
        _per_person = [
            (_generate_timeline,             'timeline      '),
            (_generate_draft_queue,          'draft-queue   '),
        ]
        count = 0
        skipped = 0
        profiles_touched: list[Path] = []
        # The profiles actually rewritten: only these need their search text
        # re-synced. Re-reading every profile looked at made a quiet refresh
        # on a large archive take minutes.
        profiles_written: set[Path] = set()
        companion_ids = set(person_ids)
        for pid in list(person_ids) + region_only_ids:
            for fn, label in (_per_person if pid in companion_ids else ()):
                for fmt_pass in fmt_passes:
                    out = _generate_or_warn(
                        f'{label.strip()} for {pid}', fn, conn, pid, archive_root, fmt=fmt_pass,
                    )
                    if out:
                        print(f'  {label}->{out.relative_to(archive_root)}')
                        changed.append(str(out))
                        count += 1
                    else:
                        skipped += 1
            # Sources-index (#76): the per-person form is a region INSIDE the
            # profile now, not a companion file, so it has no html twin -
            # refresh it once per person, only on the 'md' pass, never once
            # per fmt_pass (an 'html'/'both' pass would otherwise call this
            # again for no reason, bumping the region's date without anything
            # about it actually changing).
            if 'md' in fmt_passes:
                out = _generate_or_warn(
                    f'sources-index for {pid}', _generate_sources_index_person,
                    conn, pid, archive_root, fmt='md', written=profiles_written,
                )
                if out:
                    print(f'  sources-index ->{out.relative_to(archive_root)}')
                    changed.append(str(out))
                    profiles_touched.append(out)
                    count += 1
                else:
                    skipped += 1
                # The summary region (SPEC §16) is the profile's other
                # generated region: same once-per-person, md-only rule.
                out = _generate_or_warn(
                    f'summary for {pid}', _generate_summary_person,
                    conn, pid, archive_root, completeness=records,
                    written=profiles_written,
                )
                if out:
                    print(f'  summary       ->{out.relative_to(archive_root)}')
                    if out not in profiles_touched:
                        changed.append(str(out))
                        profiles_touched.append(out)
                    count += 1
                else:
                    skipped += 1

        print(f'Generated {count} view file(s).')
        sync = 'indexed'
        # Refresh writes new/updated COMPANION files. That does NOT stale the
        # index (#37: generated views are excluded from the freshness
        # watermark), so their index rows - the searchable body in notes_fts
        # and the person_files entry - are rewritten here rather than waiting
        # for a rebuild nothing would prompt. `profiles_touched` (#76's
        # per-person ## Sources rewrites) needs the DIFFERENT sync
        # `resync_person_profile_rows` performs - a profile is not a
        # companion, so #37's exclusion never covered it, but the edit is
        # generated-view content in substance and must be just as freshness-
        # neutral (see that function's docstring for why leaving it to the
        # ordinary reindex reminder would break the very next `fha views`
        # call in the same session).
        companion_writes = [Path(c) for c in changed if Path(c) not in profiles_touched]
        if count and 'md' in fmt_passes and companion_writes:
            sync = _sync_written_views(archive_root, conn, companion_writes, 'files')
        if count and 'md' in fmt_passes and profiles_written:
            profile_sync = resync_person_profile_rows(
                archive_root, [p for p in profiles_touched if p in profiles_written])
            if profile_sync != 'indexed' and sync == 'indexed':
                sync = profile_sync
        # Re-pointed place links edited records the index read (see
        # `run_places`): the batch contract's index_stale, not a clean exit.
        result = _batch_view_result(count, skipped + place_skipped,
                                    changed + place_changed, 'view',
                                    'index_error' if place_relinked else sync)
        if place_relinked:
            print('Run `fha index` now: the place links re-pointed above changed '
                  'records the index had read.')
        if not person_ids and result.exit_code == EXIT_CLEAN:
            # Every curated record parked under people/stubs/: the profiles'
            # regions were refreshed, but no companion view could be, and
            # that must not read as up to date.
            parked = _empty_curated_views_result(conn)
            if parked.exit_code != EXIT_CLEAN:
                result.exit_code = parked.exit_code
        return result

    except GeneratedFileRefused as e:
        return _views_result(_refused_exit(e))
    except GeneratedFileParentMissing as e:
        return _views_result(_missing_parent_exit(e))
    except OSError as e:
        return _views_result(_write_os_error_exit(e))
    except RuntimeError as e:
        return _views_result(_runtime_error_exit(e))
    finally:
        conn.close()


def _cmd_refresh(args: argparse.Namespace) -> int:
    """argparse → run_refresh bridge; returns the process exit code."""
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return run_refresh(
        archive_root,
        fmt=getattr(args, 'format', 'md') or 'md',
    ).exit_code


def _cmd_views_help(args: argparse.Namespace) -> int:
    # Retrieve the views sub-parser and print its help via the stored reference
    parser = getattr(args, '_views_parser', None)
    if parser:
        parser.print_help()
    else:
        print('Usage: fha views <subcommand> [options]')
        print('Subcommands: timeline, sources-index, summary, draft-queue, places, '
              'brackets, tree, clean, refresh')
    # Bare `fha views` (no verb) is a usage error, not clean success: exit 2,
    # matching `fha person`/`fha confirm`/`fha places` (audit flag 15).
    return EXIT_ERRORS


# ── Printed reports ───────────────────────────────────────────────────────────
#
# A printed report is a page to hand to a relative who has no computer. It
# follows the shared-site rules for people and evidence (SPEC §21, "Printed
# reports"), every one of them asked through `_lib`'s export-privacy readers
# (`export_privacy`, `person_withheld`, `export_tie_ok`) so a report and the
# GEDCOM file agree about who is private. The shell here (privacy, notes,
# facts, rendering) is shared by every report kind; `fha views family-sheet`
# is the first.

_REPORT_BANNER = (
    'Prepared for family. Living relatives are counted but not named, and '
    'private records are left out. A number after a fact points to the record '
    'it comes from, listed at the end.'
)
_REPORT_WITHHELD_NOTE = (
    'A record that is not included in this report (it names a living relative '
    'or is private).'
)
# What a living or unknown relative is called on a printed page (SPEC §21);
# never a surname, which says who the person is related to.
_REPORT_LIVING_LABEL = 'Living Person'
_REPORT_WITHHELD_KEY = '\x00withheld'

_PERSON_SHOWN, _PERSON_LIVING, _PERSON_RESTRICTED = 'shown', 'living', 'restricted'


class _ReportPrivacy:
    """Who and what a printed report may show, read once per run.

    One place decides, for the whole report: a person is shown, is a living
    or unknown relative (counted, never named), or asked to be left out
    (restricted: not named, not counted). A claim is shown only when it is
    accepted, not negated, not marked restricted, on a source an export may
    use, and names nobody the report withholds. Keeping this in one class is
    deliberate: a later change to which claims a report may keep (a claim
    naming a deceased person beside a living one) edits `_claim_shown`, and
    every report inherits it.
    """

    def __init__(self, conn: sqlite3.Connection, archive_root: Path):
        self.conn = conn
        self.archive_root = Path(archive_root)
        self.privacy = export_privacy(conn, archive_root)
        self._rows = {
            normalize_id(str(r['id'])): r for r in conn.execute(
                'SELECT id, name, sex, living, status, merged_into, path FROM persons')}
        self._states: dict[str, str] = {}
        self._claims: dict[str, bool] = {}
        self._sources: dict[str, tuple[bool, bool]] = {}
        # Files left out only because they would not read: a report names the
        # ones it actually left something out for, with the fix, so a missing
        # grandmother is never a silent mystery (or a subject "marked private").
        bad = set(self.privacy.unreadable_persons)
        self._unreadable_by_person: dict[str, set[str]] = {}
        for r in self._rows.values():
            if r['path'] in bad:
                key = self.canon(r['id']) or normalize_id(str(r['id']))
                self._unreadable_by_person.setdefault(key, set()).add(str(r['path']))
        self._unreadable_sources = dict(self.privacy.unreadable_sources)
        self.unreadable_hit: set[str] = set()

    def lookup(self, pid):
        return self._rows.get(normalize_id(str(pid or '')))

    def canon(self, pid) -> str | None:
        """The person an id stands for after merges; None when the index has
        no such person (or a merge chain with no survivor to read)."""
        row = self.lookup(pid)
        if row is None:
            return None
        survivor = merge_survivor(merge_chain(row, self.lookup))
        return None if survivor is None else normalize_id(str(survivor['id']))

    def state(self, pid) -> str:
        key = normalize_id(str(pid or ''))
        if key not in self._states:
            row = self.lookup(key)
            if row is None:
                # An id the index does not hold is not known to be dead.
                self._states[key] = _PERSON_LIVING
            elif person_restricted(row, self.privacy, self.lookup):
                self._states[key] = _PERSON_RESTRICTED
                self.unreadable_hit |= self.unreadable_paths(key)
            elif person_withheld(self.conn, row, self.privacy, self.lookup):
                self._states[key] = _PERSON_LIVING
            else:
                self._states[key] = _PERSON_SHOWN
        return self._states[key]

    def name(self, pid) -> str:
        """The name to print: the real one for a shown person, the shared
        label for a living or unknown one."""
        if self.state(pid) != _PERSON_SHOWN:
            return _REPORT_LIVING_LABEL
        row = self.lookup(pid)
        return str(row['name']) if row is not None else _REPORT_LIVING_LABEL

    def sex(self, pid) -> str:
        row = self.lookup(pid)
        return str(row['sex'] or '') if row is not None else ''

    def unreadable_paths(self, pid) -> set[str]:
        """The person files that would not read and so made `pid` restricted
        (their own, or one merged into the same survivor)."""
        key = self.canon(pid) or normalize_id(str(pid or ''))
        return set(self._unreadable_by_person.get(key, ()))

    def unreadable_warnings(self) -> list[str]:
        """One warning per file this report left something out for only
        because it would not read, saying what is wrong and how to fix it."""
        out = []
        for path in sorted(self.unreadable_hit):
            _value, trouble = read_restriction_marker(self.archive_root / path, missing_ok=True)
            out.append(
                f'WARNING: could not read {path} for its privacy marker'
                + (f' ({trouble})' if trouble else '')
                + ', so there is no way to tell whether it was marked private '
                '(restricted:). Everything it touches was left out of this report. '
                'Repair the file (`fha lint` points at what is wrong), run '
                '`fha index`, then print the report again.')
        return out

    def source_info(self, sid: str) -> tuple[bool, bool]:
        """`(public, citable)` for a source. Public: an export may use it and it
        names no restricted person. Citable: public, and it names no living or
        unknown person either (a source withheld only for that reason is
        cited by the shared label, never by its title)."""
        if sid not in self._sources:
            row = self.conn.execute(
                f"SELECT {public_source_sql('s')} FROM sources s WHERE s.id = ?",
                (sid,)).fetchone()
            public = (row is None or bool(row[0])) and sid not in self.privacy.restricted_sources
            if sid in self._unreadable_sources:
                self.unreadable_hit.add(str(self._unreadable_sources[sid]))
            ids = {r[0] for r in self.conn.execute(
                'SELECT person_id FROM source_people WHERE source_id = ? '
                'UNION SELECT cp.person_id FROM claim_persons cp '
                'JOIN claims c ON c.id = cp.claim_id WHERE c.source_id = ?', (sid, sid))}
            states = {self.state(i) for i in ids if i}
            citable = public and _PERSON_RESTRICTED not in states and _PERSON_LIVING not in states
            public = public and _PERSON_RESTRICTED not in states
            self._sources[sid] = (public, citable)
        return self._sources[sid]

    def claim_shown(self, claim_id: str) -> bool:
        if claim_id not in self._claims:
            self._claims[claim_id] = self._claim_shown(claim_id)
        return self._claims[claim_id]

    def _claim_shown(self, claim_id: str) -> bool:
        row = self.conn.execute(
            'SELECT status, COALESCE(negated, 0), source_id FROM claims WHERE id = ?',
            (claim_id,)).fetchone()
        if row is None or row[0] != 'accepted' or row[1]:
            return False
        if normalize_id(str(claim_id)) in self.privacy.restricted_claims:
            return False
        if row[2] and not self.source_info(row[2])[0]:
            return False
        return all(self.state(r[0]) == _PERSON_SHOWN for r in self.conn.execute(
            'SELECT person_id FROM claim_persons WHERE claim_id = ?', (claim_id,)))

    def ties(self, pid: str, rel: str) -> dict[str, dict]:
        """The ties of one kind a report may walk from `pid`: `{other: {date,
        subtypes}}`. A tie through a living or unknown relative is kept (they
        are never named); one with a restricted person at either end, or one
        with no accepted claim behind it, is cut (`export_tie_ok`)."""
        out: dict[str, dict] = {}
        for r in self.conn.execute(
                'SELECT r.person_id, r.other_id, r.claim_id, r.date_start, c.subtype, c.source_id '
                'FROM relationships r LEFT JOIN claims c ON c.id = r.claim_id '
                'WHERE r.person_id = ? AND r.rel = ?', (pid, rel)):
            if not export_tie_ok(self.conn, r, self.privacy, self.lookup, unclaimed_ok=False):
                # Asking each person on the cut tie notes any file that cut
                # it only because it would not read (the run warns about it).
                for who in (r['person_id'], r['other_id']):
                    self.state(who)
                if r['claim_id']:
                    for cp in self.conn.execute(
                            'SELECT person_id FROM claim_persons WHERE claim_id = ?',
                            (r['claim_id'],)):
                        self.state(cp[0])
                    if r['source_id']:
                        self.source_info(r['source_id'])
                continue
            other = self.canon(r['other_id'])
            if other is None or other == pid:
                continue
            # `sources`: the records the tie rests on, cited beside it so a
            # relative can look up where "his mother was Ann Price" comes from.
            info = out.setdefault(other, {'date': None, 'subtypes': [], 'sources': set()})
            if r['source_id']:
                info['sources'].add(r['source_id'])
            start = r['date_start'] or None
            if start and (info['date'] is None or start < info['date']):
                info['date'] = start
            if rel in ('parent', 'child'):
                info['subtypes'].append((r['subtype'] or '').strip().lower())
        return out

    def parented_by(self, pid: str) -> list[str]:
        """People whose own parent tie names `pid`: a child recorded only from
        the child's side still belongs on the parent's sheet."""
        found = []
        for r in self.conn.execute(
                "SELECT DISTINCT person_id FROM relationships "
                "WHERE other_id = ? AND rel = 'parent'", (pid,)):
            child = self.canon(r[0])
            if child and child != pid:
                found.append(child)
        return found

    def co_parents(self, pid: str) -> set[str]:
        """The other birth parents of `pid`'s birth children, through ties a
        report may walk (so never a restricted person). Only birth ties on
        both sides: an adopted child's birth mother or a stepchild's father
        is not his partner, and must never be printed beside him."""
        def genetic(info) -> bool:
            return bool(info) and any(is_genetic_parent_subtype(s) for s in info['subtypes'])

        mine = self.ties(pid, 'child')
        children = set(mine) | set(self.parented_by(pid))
        out: set[str] = set()
        for c in children:
            theirs = self.ties(c, 'parent')
            if not (genetic(mine.get(c)) or genetic(theirs.get(pid))):
                continue
            out |= {p for p, info in theirs.items() if p != pid and genetic(info)}
        return out

    def other_parent_recorded(self, child: str, parent: str) -> bool:
        """Whether the records name any parent of `child` besides `parent`,
        private or not. A child with another recorded parent is not that
        parent's child with a different spouse, so must not be printed under
        a couple it does not belong to."""
        for r in self.conn.execute(
                "SELECT other_id FROM relationships WHERE person_id = ? AND rel = 'parent' "
                "UNION SELECT person_id FROM relationships WHERE other_id = ? AND rel = 'child'",
                (child, child)):
            other = self.canon(r[0]) or normalize_id(str(r[0] or ''))
            if other and other != parent:
                return True
        return False


class _Footnotes:
    """Numbers the records a report cites, in order of first citation. All the
    records the report may not name share one note, as on the site."""

    def __init__(self, rp: _ReportPrivacy):
        self.rp = rp
        self.order: list[str] = []

    def cite(self, sid: str) -> int:
        key = sid if self.rp.source_info(sid)[1] else _REPORT_WITHHELD_KEY
        if key not in self.order:
            self.order.append(key)
        return self.order.index(key) + 1

    def lines(self) -> list[str]:
        out = []
        for n, key in enumerate(self.order, 1):
            if key == _REPORT_WITHHELD_KEY:
                out.append(f'{n}. {_REPORT_WITHHELD_NOTE}')
                continue
            row = self.rp.conn.execute(
                'SELECT title, path FROM sources WHERE id = ?', (key,)).fetchone()
            title = ' '.join(str(row['title'] if row else key).split()).rstrip('.')
            citation = ''
            if row is not None and row['path']:
                try:
                    rec = read_record(self.rp.archive_root / row['path'],
                                      on_decode_error=lambda _p: None)
                    citation = ' '.join(str(rec['meta'].get('citation') or '').split())
                except Exception:   # noqa: BLE001 - an unreadable record gives the title alone
                    citation = ''
            out.append(f'{n}. {title}. {citation}' if citation else f'{n}. {title}.')
        return out


_VITAL_COLS = ('c.id, c.source_id, c.date_edtf, c.date_min, c.date_max, '
               'c.place_id, c.place_text, c.confidence')


def _report_pick_vital(rp: _ReportPrivacy, pid: str, ctype: str):
    """The one claim a report prints for this person's birth, baptism, death
    or burial: accepted, a record OF this person, shown, and the best by the
    rule the site and `fha views summary` use (`vital_pick_key`)."""
    rows = rp.conn.execute(
        f'SELECT DISTINCT {_VITAL_COLS} FROM claims c '
        'JOIN claim_persons cp ON cp.claim_id = c.id '
        "WHERE cp.person_id = ? AND c.type = ? AND c.status = 'accepted' "
        'AND COALESCE(c.negated, 0) = 0', (pid, ctype)).fetchall()
    good = [r for r in rows
            if claim_is_own_vital(rp.conn, pid, r['id'], ctype) and rp.claim_shown(r['id'])]
    return min(good, key=vital_pick_key) if good else None


def _report_place(rp: _ReportPrivacy, claim) -> str:
    if claim['place_text']:
        return str(claim['place_text'])
    if claim['place_id']:
        row = rp.conn.execute('SELECT name FROM places WHERE id = ?',
                              (claim['place_id'],)).fetchone()
        if row and row['name']:
            return str(row['name'])
    return ''


def _fact_text(rp: _ReportPrivacy, fn: _Footnotes, claim, *, with_place: bool = True) -> str:
    """`14 June 1840, Fairview, Breton County, Ohio [1]`, or '' when the claim
    gives neither a date nor a place worth printing."""
    date = humanize_edtf(claim['date_edtf']) if claim['date_edtf'] else ''
    place = _report_place(rp, claim) if with_place else ''
    body = ', '.join(x for x in (date, place) if x)
    return f"{body} [{fn.cite(claim['source_id'])}]" if body else ''


def _fact_line(rp: _ReportPrivacy, fn: _Footnotes, label: str, claim) -> str | None:
    """`Born: 14 June 1840, Fairview, Breton County, Ohio [1]`, or None."""
    text = _fact_text(rp, fn, claim)
    return f'{label}: {text}' if text else None


def _living_count_phrase(n: int, noun_one: str, noun_many: str) -> str:
    """`and 1 living child (not named)`, `and 3 living grandchildren (not named)`."""
    return f'and {n} living {noun_one if n == 1 else noun_many} (not named)'


_PLAIN_DATE_RE = re.compile(r'^\d{4}(-\d{2}){0,2}$')


def _claim_year(claim) -> str:
    """The year printed beside a parent's name: the bare year for a plain
    date, the date in words when the record hedges it (`about 1815`,
    `1818 to 1820`), so a relative never reads an exact year the records do
    not give."""
    edtf = str(claim['date_edtf'] or '').strip()
    if _PLAIN_DATE_RE.match(edtf):
        return edtf[:4]
    return humanize_edtf(edtf) if edtf else ''


def _cite_all(fn: '_Footnotes', sources) -> str:
    """` [3, 5]` for the records a line rests on, or ''."""
    notes = sorted({fn.cite(sid) for sid in sources if sid})
    return f" [{', '.join(str(n) for n in notes)}]" if notes else ''


class _SheetTally:
    """Who a sheet names, and how many living relatives it counts."""

    def __init__(self, rp: _ReportPrivacy):
        self.rp = rp
        self.named: set[str] = set()
        self.living: set[str] = set()

    def see(self, pid: str) -> str:
        state = self.rp.state(pid)
        if state == _PERSON_SHOWN:
            self.named.add(pid)
        elif state == _PERSON_LIVING:
            self.living.add(pid)
        return state


_SEX_ORDER = {'M': 0, 'F': 1}


def _report_parent_lines(rp: _ReportPrivacy, fn: _Footnotes, tally: _SheetTally,
                         pid: str) -> list[str]:
    """`Father: John Hartley (1810-1881) [5, 6]` for each parent: birth
    parents first, then an adoptive, step or other parent labelled so
    (`Father (adoptive): ...`). Each line cites the record of the tie and of
    the years. A living parent is the shared label; a restricted one is left
    out."""
    ties = rp.ties(pid, 'parent')

    def genetic(p: str) -> bool:
        return any(is_genetic_parent_subtype(s) for s in ties[p]['subtypes'])

    parents = sorted(ties, key=lambda p: (not genetic(p), _SEX_ORDER.get(rp.sex(p), 2), p))
    lines = []
    for other in parents:
        state = tally.see(other)
        if state == _PERSON_RESTRICTED:
            continue
        label = {'M': 'Father', 'F': 'Mother'}.get(rp.sex(other), 'Parent')
        if not genetic(other):
            kind = next((s for s in ties[other]['subtypes'] if s), '')
            label += f' ({kind})' if kind else ''
        sources = set(ties[other]['sources'])
        if state == _PERSON_LIVING:
            lines.append(f'{label}: {_REPORT_LIVING_LABEL}{_cite_all(fn, sources)}')
            continue
        born = _report_pick_vital(rp, other, 'birth')
        died = _report_pick_vital(rp, other, 'death')
        by, dy = (_claim_year(born) if born else ''), (_claim_year(died) if died else '')
        years = ''
        if by.isdigit() and dy.isdigit():
            years = f' ({by}-{dy})'
        elif by and dy:
            years = f' (born {by}, died {dy})'
        elif by:
            years = f' (born {by})'
        elif dy:
            years = f' (died {dy})'
        sources |= {c['source_id'] for c in (born, died) if c and _claim_year(c)}
        lines.append(f'{label}: {rp.name(other)}{years}{_cite_all(fn, sources)}')
    return lines


_VITAL_ROWS = (('Born', 'birth'), ('Baptised', 'baptism'), ('Died', 'death'), ('Buried', 'burial'))


def _report_person_block(rp: _ReportPrivacy, fn: _Footnotes, tally: _SheetTally,
                         heading: str, label: str, pid: str) -> list[str]:
    state = tally.see(pid)
    lines = [f'{heading} {label}: {rp.name(pid)}', '']
    if state != _PERSON_SHOWN:
        return lines + ['Details are not shown because this person may be living.', '']
    for word, ctype in _VITAL_ROWS:
        claim = _report_pick_vital(rp, pid, ctype)
        line = _fact_line(rp, fn, word, claim) if claim else None
        if line:
            lines.append(line)
    lines += _report_parent_lines(rp, fn, tally, pid)
    lines.append('')
    return lines


def _report_marriage(rp: _ReportPrivacy, subject: str, spouse: str):
    rows = rp.conn.execute(
        f'SELECT DISTINCT {_VITAL_COLS} FROM relationships r JOIN claims c ON c.id = r.claim_id '
        "WHERE r.person_id = ? AND r.other_id = ? AND r.rel = 'spouse' "
        "AND c.type = 'marriage' AND c.status = 'accepted' AND COALESCE(c.negated, 0) = 0",
        (subject, spouse)).fetchall()
    good = [r for r in rows if rp.claim_shown(r['id'])]
    return min(good, key=vital_pick_key) if good else None


def _report_children(rp: _ReportPrivacy, subject: str, spouse: str | None,
                     all_spouses: list[str], first_sheet: bool) -> list[tuple[str, dict]]:
    """The children this sheet lists, as `(child id, tie info)`: the couple's
    own, plus (on the subject's first sheet only) children with no other
    parent recorded at all. A child whose other recorded parent is not this
    spouse is left off rather than printed as this couple's child. With no
    spouse on record, every child of the subject is listed."""
    children_of: dict[str, dict] = {}
    parents_of: dict[str, dict] = {}

    def kids(p: str) -> dict:
        if p not in children_of:
            children_of[p] = rp.ties(p, 'child')
        return children_of[p]

    def parents(c: str) -> dict:
        if c not in parents_of:
            parents_of[c] = rp.ties(c, 'parent')
        return parents_of[c]

    def is_child_of(p: str, c: str) -> bool:
        return c in kids(p) or p in parents(c)

    candidates = dict(kids(subject))
    if spouse:
        for c, info in kids(spouse).items():
            candidates.setdefault(c, info)
    for c in rp.parented_by(subject):
        candidates.setdefault(c, {'date': None, 'subtypes': list(parents(c).get(subject, {}).get('subtypes', []))})
    def tie_info(c: str, ps: list[str]) -> dict:
        # The subject's own tie decides the (adopted) mark; every record of
        # the tie, from either side and either parent, is cited.
        own = [kids(subject).get(c), parents(c).get(subject)]
        subtypes = [s for t in own if t for s in t['subtypes']]
        sources: set[str] = set()
        for p in ps:
            for t in (kids(p).get(c), parents(c).get(p)):
                if t:
                    sources |= t['sources']
        return {'date': None, 'subtypes': subtypes, 'sources': sources}

    out = []
    for c in candidates:
        if not is_child_of(subject, c):
            continue
        if spouse and is_child_of(spouse, c):
            out.append((c, tie_info(c, [subject, spouse])))
        elif not all_spouses or (first_sheet and not rp.other_parent_recorded(c, subject)):
            out.append((c, tie_info(c, [subject])))
        elif first_sheet and not (set(parents(c)) & set(all_spouses)):
            # A child he adopted alone, or whose other parent is private,
            # shares no sheet with any partner of his and would vanish from
            # every sheet: keep her on his first one, marked as his alone so
            # the spouse is not implied (the private parent is never named).
            info = tie_info(c, [subject])
            info['only'] = subject
            out.append((c, info))
    return out


def _family_sheet_lines(rp: _ReportPrivacy, subject: str, spouse: str | None,
                        all_spouses: list[str], first_sheet: bool,
                        top: int, married: bool = True) -> tuple[str, list[str]]:
    """One couple's sheet as markdown: `(title, lines)`. `top` is the heading
    level of the sheet title (1 alone, 2 when a file holds several sheets)."""
    fn = _Footnotes(rp)
    tally = _SheetTally(rp)
    h2 = '#' * (top + 1)
    couple = [subject] + ([spouse] if spouse else [])
    couple.sort(key=lambda p: _SEX_ORDER.get(rp.sex(p), 2))   # husband first; ties keep the order
    title_names = ' and '.join(rp.name(p) for p in couple)
    body: list[str] = []
    for p in couple:
        # Two parents with no marriage on record are Father and Mother,
        # never Husband and Wife: the sheet must not state a marriage.
        # With no spouse on record she is never called a wife either.
        if married and spouse:
            label = {'M': 'Husband', 'F': 'Wife'}.get(rp.sex(p), 'Spouse')
        elif spouse or rp.ties(p, 'child') or rp.parented_by(p):
            label = {'M': 'Father', 'F': 'Mother'}.get(rp.sex(p), 'Parent')
        else:
            label = 'Person'
        body += _report_person_block(rp, fn, tally, h2, label, p)
    if not spouse:
        body += [f'{h2} Spouse', '', 'No spouse is recorded.', '']
    else:
        marriage = _report_marriage(rp, subject, spouse)
        line = _fact_line(rp, fn, 'Married', marriage) if marriage else None
        if line:
            body += [f'{h2} Marriage', '', line, '']

    listed, living_n = [], 0
    for c, info in _report_children(rp, subject, spouse, all_spouses, first_sheet):
        state = tally.see(c)
        if state == _PERSON_LIVING:
            living_n += 1
        elif state == _PERSON_SHOWN:
            birth = _report_pick_vital(rp, c, 'birth')
            listed.append((birth['date_min'] if birth and birth['date_min'] else '9999',
                           rp.name(c), c, info, birth))
    listed.sort(key=lambda t: t[:3])
    if listed or living_n:
        body += [f'{h2} Children', '']
    if listed:
        body += ['| | Name | Born | Died | Married |', '|---|---|---|---|---|']
        for n, (_key, name, c, info, birth) in enumerate(listed, 1):
            subtypes = info.get('subtypes') or []
            mark = ''
            kind = None
            if subtypes and not any(is_genetic_parent_subtype(s) for s in subtypes):
                kind = nonbirth_bracket_label(subtypes[0])
            if spouse and info.get('only'):
                only = rp.name(info['only'])
                kind = f'{kind} child of {only}' if kind else f'child of {only}'
            mark = f' ({kind})' if kind else ''
            born_cell = _fact_text(rp, fn, birth) if birth else ''
            death = _report_pick_vital(rp, c, 'death')
            died_cell = _fact_text(rp, fn, death, with_place=False) if death else ''
            spouse_ties = rp.ties(c, 'spouse')
            married = [rp.name(sp) + _cite_all(fn, spouse_ties[sp]['sources'])
                       for sp in sorted(spouse_ties)
                       if tally.see(sp) != _PERSON_RESTRICTED]
            cells = [name + mark + _cite_all(fn, info.get('sources') or ()),
                     born_cell, died_cell, '; '.join(married)]
            body.append(f'| {n} | ' + ' | '.join(x.replace('|', '/') for x in cells) + ' |')
        body.append('')
    if living_n:
        phrase = _living_count_phrase(living_n, 'child', 'children')
        body += [phrase if listed else phrase[len('and '):], '']
    notes = fn.lines()
    if notes:
        body += [f'{h2} Notes', ''] + notes + ['']

    k = len(tally.living)
    summary = f'This sheet names {len(tally.named)} people.'
    if k:
        summary += (f' {k} living relatives are counted but not named.' if k != 1
                    else ' 1 living relative is counted but not named.')
    return title_names, ['#' * top + f' Family group sheet: {title_names}', '', summary, ''] + body


_FACT_LABEL_RE = re.compile(r'^([A-Z][A-Za-z ]{1,20}): (.*)$')
_NOTE_REF_RE = re.compile(r' \[(\d+(?:, \d+)*)\]')


def _report_inline_html(text: str) -> str:
    return _NOTE_REF_RE.sub(lambda m: f'<sup class="note">{m.group(1)}</sup>',
                            html_escape(text))


_REG_CHILD_RE = re.compile(r'^- (?:(\d+) )?((?:[ivxlcdm]+\. |and ).*)$')


def _report_md_to_html(lines: list[str]) -> tuple[str, str]:
    """`(title, body_html)` for a report written as the small markdown the
    report builders produce (headings, fact lines, one table, a numbered notes
    list, `---` between sheets). The first heading becomes the page title and
    the purpose blockquote is dropped: the page carries it as its banner. Every
    piece of text is escaped here."""
    title = ''
    parts = ['<section class="report-sheet">']
    mode = ''   # '', 'table' or 'ol'

    def close() -> None:
        nonlocal mode
        if mode == 'table':
            parts.append('</tbody></table>')
        elif mode == 'ol':
            parts.append('</ol>')
        mode = ''

    for line in lines:
        if not line.strip() or line.startswith('>'):
            close()
            continue
        if line.strip() == '---':
            close()
            parts.append('</section><section class="report-sheet">')
            continue
        m = re.match(r'^(#{1,6}) (.*)$', line)
        if m:
            close()
            if not title and len(m.group(1)) == 1:
                title = m.group(2)
                continue
            level = len(m.group(1))
            parts.append(f'<h{level}>{html_escape(m.group(2))}</h{level}>')
            continue
        if line.startswith('|'):
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if all(re.fullmatch(r'-+', c) for c in cells):
                continue
            if mode != 'table':
                close()
                head = ''.join(f'<th>{html_escape(c)}</th>' for c in cells)
                parts.append(f'<table class="claims"><thead><tr>{head}</tr></thead><tbody>')
                mode = 'table'
                continue
            parts.append('<tr>' + ''.join(
                f'<td>{_report_inline_html(c)}</td>' for c in cells) + '</tr>')
            continue
        m = re.match(r'^\*\*(\d+)\.\*\* (.*)$', line)
        if m:
            # A numbered person (the Ahnentafel report), not a note.
            close()
            parts.append(f'<p class="ahn-person"><strong>{m.group(1)}.</strong> '
                         f'{_report_inline_html(m.group(2))}</p>')
            continue
        m = re.match(r'^(\d+)\. (.*)$', line)
        if m:
            if mode != 'ol':
                close()
                parts.append('<ol class="report-notes">')
                mode = 'ol'
            parts.append(f'<li value="{m.group(1)}">{html_escape(m.group(2))}</li>')
            continue
        child = _REG_CHILD_RE.match(line)
        if child:
            # A child in a register family: `- 2 i. John ...` (the number only
            # when the child's own family follows) or `- and 2 living ...`.
            close()
            num = f'<strong>{child.group(1)}</strong> ' if child.group(1) else ''
            parts.append(f'<p class="report-child">{num}{_report_inline_html(child.group(2))}</p>')
            continue
        close()
        bullet = line.startswith('- ')
        m = _FACT_LABEL_RE.match(line[2:] if bullet else line)
        if m:
            parts.append(f'<p class="fact{" ahn-fact" if bullet else ""}"><strong>{html_escape(m.group(1))}:</strong> '
                         f'{_report_inline_html(m.group(2))}</p>')
        else:
            parts.append(f'<p>{_report_inline_html(line)}</p>')
    close()
    parts.append('</section>')
    return title, '\n'.join(parts) + '\n'


def _render_report(archive_root: Path, kind: str, pid: str, lines: list[str], fmt: str) -> Path:
    """Write one report to generated/views/ as `report_{kind}_{p-id}.{md|html}`:
    the GENERATED marker, then the report. Returns the path."""
    lines = [ln for i, ln in enumerate(lines) if ln or (i and lines[i - 1])]   # one blank at most
    out_dir = archive_root / 'generated' / 'views'
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = _existing_spelling(out_dir / f'report_{kind}_{fmt_id_display(pid)}.{fmt}')
    if fmt == 'html':
        title, body = _report_md_to_html(lines)
        text = _render_view_html(archive_root, kind, title, body, banner=_REPORT_BANNER)
    else:
        first = next(i for i, ln in enumerate(lines) if ln.startswith('# '))
        text = (_gen_header(kind) + '\n'.join(lines[:first + 1]) + '\n\n> '
                + _REPORT_BANNER + '\n' + '\n'.join(lines[first + 1:]) + '\n')
    return _write_view_file(out_path, text)


def _pronouns(sex: str) -> tuple[str, str]:
    return {'F': ('she', 'her'), 'M': ('he', 'him')}.get(sex, ('they', 'them'))


def run_family_sheet(
    archive_root: Path,
    person_id: str | None,
    spouse_id: str | None = None,
    fmt: str = 'md',
) -> Result:
    """Write the printed family group sheet for one person (and one spouse, or
    one sheet per spouse). Exit codes: 0 written; 1 person not in the index;
    2 refused (living or private subject, or a spouse who is not one); 3 no
    usable index."""
    precheck = _format_precheck(fmt, ('md', 'html'))
    if precheck is not None:
        return precheck
    conn = _open_index_or_explain(archive_root)
    if conn is None:
        return _views_result(EXIT_FAILURE)
    try:
        rp = _ReportPrivacy(conn, archive_root)
        pid = rp.canon(person_id or '')
        if pid is None:
            print(f'No person {fmt_id_display(person_id or "")} in the index. '
                  f'Run fha find "their name" to look them up by name, or fha index '
                  f'if you just added them.', file=sys.stderr)
            return _views_result(EXIT_WARNINGS)
        shown_id = fmt_id_display(pid)
        state = rp.state(pid)
        if state == _PERSON_RESTRICTED:
            unread = sorted(rp.unreadable_paths(pid))
            if unread:
                # A damaged file is not a request for privacy: say which file
                # and the fix, never "marked private".
                print(f'Could not read {", ".join(unread)} to check whether {shown_id} '
                      f'asked to be kept private, so no report about them can be printed. '
                      f'Nothing was written. Repair the file (`fha lint` points at '
                      f'what is wrong), run `fha index`, then try again.', file=sys.stderr)
            else:
                print(f'{shown_id} is marked private (restricted), so it is left out of '
                      f'printed reports. Nothing was written.', file=sys.stderr)
            return _views_result(EXIT_ERRORS)
        name = rp.lookup(pid)['name']
        if state == _PERSON_LIVING:
            they, them = _pronouns(rp.sex(pid))
            has = 'have' if they == 'they' else 'has'
            print(f'{name} is recorded as living, or nobody has recorded yet whether '
                  f'{they} died, so a printed report about {them} would name a living '
                  f'person. Choose a deceased ancestor instead. If {they} {has} died, '
                  f'record it first: fha person set-living {shown_id} false',
                  file=sys.stderr)
            return _views_result(EXIT_ERRORS)

        spouse_ties = rp.ties(pid, 'spouse')
        all_spouses = sorted(
            (s for s in spouse_ties if rp.state(s) != _PERSON_RESTRICTED),
            key=lambda s: (spouse_ties[s]['date'] is None, spouse_ties[s]['date'] or '', s))
        # The other parent of his children with no marriage on record (a
        # birth record naming both, no marriage found) gets a sheet too,
        # after the spouses, so those children are neither lost nor printed
        # under a wife who was not their mother.
        all_spouses += sorted(
            p for p in rp.co_parents(pid)
            if p not in spouse_ties and rp.state(p) != _PERSON_RESTRICTED)
        if spouse_id:
            sp = rp.canon(spouse_id)
            if sp is None or sp not in all_spouses:
                known = ', '.join(rp.name(s) for s in all_spouses) or 'none recorded'
                print(f"{fmt_id_display(spouse_id)} is not recorded as {name}'s spouse. "
                      f'Their recorded spouses are: {known}.', file=sys.stderr)
                return _views_result(EXIT_ERRORS)
            sheets = [(sp, sp == all_spouses[0])]
        else:
            sheets = [(sp, i == 0) for i, sp in enumerate(all_spouses)] or [(None, True)]

        multi = len(sheets) > 1
        built = [_family_sheet_lines(rp, pid, sp, all_spouses, first, 2 if multi else 1,
                                     married=sp is None or sp in spouse_ties)[1]
                 for sp, first in sheets]
        if multi:
            lines = [f'# Family group sheets: {name}', '']
            for i, sheet in enumerate(built):
                lines += (['---', ''] if i else []) + sheet
        else:
            lines = built[0]
        out = _render_report(archive_root, 'family-sheet', pid, lines, fmt)
        warnings = rp.unreadable_warnings()
        for warning in warnings:
            print(warning, file=sys.stderr)
        rel = _rel_display(out, archive_root)
        if fmt == 'html':
            print(f'Wrote {rel}. Open it in a browser and print it, or choose Save as PDF '
                  f'in the print box.')
        else:
            print(f'Wrote {rel}.')
        # Exit 1 when a file that would not read left something out (as
        # `fha gedcom` does), so a script never calls a sheet with a missing
        # grandmother clean.
        return _views_result(EXIT_WARNINGS if warnings else EXIT_CLEAN, changed=[str(out)])
    except GeneratedFileRefused as e:
        return _views_result(_refused_exit(e))
    except OSError as e:
        return _views_result(_write_os_error_exit(e))
    except RuntimeError as e:
        return _views_result(_runtime_error_exit(e))
    finally:
        conn.close()


# ── Register report (NGSQ style) ──────────────────────────────────────────────
#
# A relative who reads genealogy books gets everyone descended from one
# ancestor the way the National Genealogical Society Quarterly prints a
# family: each person who has children gets a number and a short paragraph of
# fixed fact sentences, children are listed with Roman numerals, and a child
# whose own family follows is marked with the number to turn to. Numbers run
# generation by generation (not down one line to the bottom first). Living
# descendants are counted under their parent and never named, dated or placed,
# and their own descendants are left out, as in every printed report.

_REG_DEFAULT_GENERATIONS = 4
_REG_MAX_GENERATIONS = 10
_ROMAN_LOWER = (('m', 1000), ('cm', 900), ('d', 500), ('cd', 400), ('c', 100), ('xc', 90),
                ('l', 50), ('xl', 40), ('x', 10), ('ix', 9), ('v', 5), ('iv', 4), ('i', 1))


def _roman_lower(n: int) -> str:
    """`i`, `ii`, `iii`, `iv` ... for a child's birth order within a family."""
    out = ''
    for glyph, value in _ROMAN_LOWER:
        while n >= value:
            out += glyph
            n -= value
    return out


def _reg_child_ties(rp: _ReportPrivacy, pid: str) -> dict[str, dict]:
    """The people whose parent tie names `pid`, from either side of the tie:
    `{child: {subtypes, sources}}`. Ties to a restricted person are already
    cut by `rp.ties`."""
    found: dict[str, dict] = {}
    for c, info in rp.ties(pid, 'child').items():
        found[c] = {'subtypes': list(info['subtypes']), 'sources': set(info['sources'])}
    for c in rp.parented_by(pid):
        info = rp.ties(c, 'parent').get(pid)
        if info is not None:
            mine = found.setdefault(c, {'subtypes': [], 'sources': set()})
            mine['subtypes'] += info['subtypes']
            mine['sources'] |= info['sources']
    return found


def _reg_is_birth(info: dict) -> bool:
    return not info['subtypes'] or any(is_genetic_parent_subtype(s) for s in info['subtypes'])


def _reg_below_living(rp: _ReportPrivacy, root: str) -> set[str]:
    """Everyone descended from a living (or not-known-dead) descendant of
    `root`. A printed page may neither name nor count them, even when their
    other parent is a deceased cousin the report does list. People on the line
    from `root` down to that living person are never among them, so a tie
    keyed backwards cannot take a branch off the page."""
    reached, living = {root}, set()
    parents: dict[str, set[str]] = {}
    frontier = [root]
    while frontier:
        following = []
        for pid in frontier:
            for c in _reg_child_ties(rp, pid):
                state = rp.state(c)
                if state == _PERSON_RESTRICTED:
                    continue
                parents.setdefault(c, set()).add(pid)
                if state == _PERSON_LIVING:
                    living.add(c)
                elif c not in reached:
                    reached.add(c)
                    following.append(c)
        frontier = following
    below: set[str] = set()
    for person in sorted(living):
        line_up, stack = set(), [person]
        while stack:
            for par in parents.get(stack.pop(), ()):
                if par not in line_up:
                    line_up.add(par)
                    stack.append(par)
        seen, frontier = set(), [person]
        while frontier:
            following = []
            for pid in frontier:
                for c in _reg_child_ties(rp, pid):
                    if c not in seen and c not in line_up and c != person:
                        seen.add(c)
                        following.append(c)
            frontier = following
        below |= seen
    return below


def _reg_other_parent(rp: _ReportPrivacy, child: str, pid: str, spouses) -> str | None:
    """The child's other birth parent, for the "Children of A and B" heading:
    from the child's own ties, else the spouse whose own child list holds
    them. None when none is recorded or the other parent is private."""
    ties = rp.ties(child, 'parent')
    found = sorted(p for p, info in ties.items() if p != pid and rp.state(p) != _PERSON_RESTRICTED
                   and any(is_genetic_parent_subtype(s) for s in info['subtypes']))
    if found:
        return found[0]
    for sp in spouses:
        if child in rp.ties(sp, 'child'):
            return sp
    return None


_REG_NONBIRTH = object()


def _reg_families(rp: _ReportPrivacy, pid: str, hidden: set[str]) -> tuple[list, int]:
    """One person's children the report may name, as families `(other parent
    or None, [(child, tie info, birth claim)])`, and how many living children
    are counted. Families run in the order the parents married (undated last),
    those with no other parent recorded last; within a family birth children
    come oldest first (undated last, then name, then id), then adopted and
    step children the same way. A child in `hidden` is neither listed nor
    counted."""
    spouses = rp.ties(pid, 'spouse')
    by_other: dict = {}
    living_n = 0
    for c, info in _reg_child_ties(rp, pid).items():
        if c in hidden:
            continue
        state = rp.state(c)
        if state == _PERSON_LIVING:
            living_n += 1
        elif state == _PERSON_SHOWN:
            birth = _report_pick_vital(rp, c, 'birth')
            other = _reg_other_parent(rp, c, pid, spouses) if _reg_is_birth(info) else _REG_NONBIRTH
            by_other.setdefault(other, []).append(
                (not _reg_is_birth(info), birth['date_min'] if birth and birth['date_min'] else '9999',
                 rp.name(c), c, info, birth))

    def family_key(other):
        if other is None:
            return (2, '', '')
        date = spouses.get(other, {}).get('date')
        return (1 if date is None else 0, date or '', other)

    # An adopted or step child is listed at the end of the first couple's
    # family, not in a family of their own that would restart the numerals.
    if _REG_NONBIRTH in by_other:
        adopted = by_other.pop(_REG_NONBIRTH)
        couples = sorted((o for o in by_other if o is not None), key=family_key)
        by_other.setdefault(couples[0] if couples else None, []).extend(adopted)
    families = []
    for other in sorted(by_other, key=family_key):
        kids = sorted(by_other[other], key=lambda t: t[:4])
        families.append((other, [(t[3], t[4], t[5]) for t in kids]))
    return families, living_n


def _reg_when(rp: _ReportPrivacy, fn: _Footnotes, claim, lead: bool = False) -> str:
    """`14 June 1840 in Fairview [4]`, or '' when the claim gives neither a
    date nor a place. With `lead`, a full date reads `on 2 May 1866` and a
    year or month reads `in 1866, in Fairview`, as a marriage sentence needs."""
    edtf = str(claim['date_edtf'] or '').strip()
    date = humanize_edtf(edtf) if edtf else ''
    joiner = ' '
    if date and lead:
        if re.match(r'^\d{4}-\d{2}-\d{2}$', edtf):
            date = 'on ' + date
        elif _PLAIN_DATE_RE.match(edtf):
            date, joiner = 'in ' + date, ', '
    place = _report_place(rp, claim)
    text = joiner.join(x for x in (date, f'in {place}' if place else '') if x)
    return f"{text} [{fn.cite(claim['source_id'])}]" if text else ''


def _reg_paragraph(rp: _ReportPrivacy, fn: _Footnotes, tally: _SheetTally, pid: str,
                   hidden: set[str]) -> str:
    """`Thomas was born 14 June 1840 in Fairview [4], and died ... [5]. He
    married Margaret Cole 2 May 1866 in Fairview [6].` Fixed fact sentences
    only; a part with nothing to say is left out, and a person with nothing
    shown gets the name and a full stop."""
    name = rp.name(pid)
    born = _report_pick_vital(rp, pid, 'birth')
    died = _report_pick_vital(rp, pid, 'death')
    b = _reg_when(rp, fn, born) if born else ''
    d = _reg_when(rp, fn, died) if died else ''
    parts = ([f'was born {b}'] if b else []) + ([f'died {d}'] if d else [])
    sentences = [f"{name} {', and '.join(parts)}." if parts else f'{name}.']
    subject = _pronouns(rp.sex(pid))[0].capitalize()
    ties = rp.ties(pid, 'spouse')
    for sp in sorted(ties, key=lambda s: (ties[s]['date'] is None, ties[s]['date'] or '', s)):
        if sp in hidden:
            continue
        state = tally.see(sp)
        if state == _PERSON_RESTRICTED:
            continue
        sources = set(ties[sp]['sources'])
        when = ''
        if state == _PERSON_SHOWN:
            marriage = _report_marriage(rp, pid, sp)
            if marriage is not None:
                when = _reg_when(rp, fn, marriage, lead=True)
                sources.add(marriage['source_id'])
        text = f'{subject} married {rp.name(sp)}'
        if when:
            sentences.append(f'{text} {when}.')
        else:
            sentences.append(f'{text}{_cite_all(fn, sources)}.')
    return ' '.join(sentences)


def _reg_child_line(rp: _ReportPrivacy, fn: _Footnotes, c: str, info: dict, birth,
                    numeral: str, number: int | None, listed_under: int | None) -> str:
    """`- 2 i. John Hartley [3], born 1867 [7]; died 1930 [8].` The number
    sits before the numeral only when the child's own family follows."""
    lead = f'- {number} {numeral}. ' if number else f'- {numeral}. '
    kind = ''
    if not _reg_is_birth(info):
        kind = f" ({nonbirth_bracket_label(info['subtypes'][0]) or info['subtypes'][0]})"
    name = f'{rp.name(c)}{kind}{_cite_all(fn, info.get("sources") or ())}'
    if listed_under is not None:
        return f'{lead}{name}, also listed in the family of no. {listed_under}.'
    facts = []
    if birth:
        text = _fact_text(rp, fn, birth, with_place=False)
        if text:
            facts.append(f'born {text}')
    death = _report_pick_vital(rp, c, 'death')
    if death:
        text = _fact_text(rp, fn, death, with_place=False)
        if text:
            facts.append(f'died {text}')
    return f"{lead}{name}, {'; '.join(facts)}." if facts else f'{lead}{name}.'


def _register_lines(rp: _ReportPrivacy, root: str, generations: int) -> list[str]:
    """The register as small markdown: a title, a summary, a `## Generation N`
    heading, then for each numbered person a paragraph and their children."""
    hidden = _reg_below_living(rp, root)
    families_of: dict[str, tuple[list, int]] = {}

    def families(pid: str):
        if pid not in families_of:
            families_of[pid] = _reg_families(rp, pid, hidden)
        return families_of[pid]

    # Number the people who carry the line, generation by generation. A person
    # met a second time (cousins who married) is listed in full once, at the
    # shortest line, and pointed to afterwards.
    number_of: dict[str, int] = {root: 1}
    depth_of: dict[str, int] = {root: 1}
    listed_in: dict[str, int] = {}          # child -> number of the family that lists them
    level = [root]
    layout: list[tuple[int, list[str]]] = []
    counter = 1
    cut = False
    for depth in range(1, generations + 1):
        layout.append((depth, level))
        following: list[str] = []
        for pid in level:
            fams, living_n = families(pid)
            for _other, kids in fams:
                for c, _info, _birth in kids:
                    if c not in depth_of:
                        depth_of[c] = depth + 1
                        listed_in[c] = number_of[pid]
                        following.append(c)
        level = []
        for c in following:
            if depth + 1 < generations and families(c)[0]:
                counter += 1
                number_of[c] = counter
                level.append(c)
        if depth + 1 >= generations:
            cut = any(families(c)[0] or families(c)[1] for c in following)
        if not level:
            break

    fn = _Footnotes(rp)
    tally = _SheetTally(rp)
    body: list[str] = []
    shown_depth = 1
    for depth, people in layout:
        if not people:
            continue
        shown_depth = max(shown_depth, depth + (1 if any(families(p)[0] for p in people) else 0))
        body += [f'## Generation {depth}', '']
        for pid in people:
            tally.see(pid)
            body += [f'**{number_of[pid]}.** {_reg_paragraph(rp, fn, tally, pid, hidden)}', '']
            fams, living_n = families(pid)
            for other, kids in fams:
                parents = rp.name(pid) + (f' and {rp.name(other)}' if other else '')
                if other:
                    tally.see(other)
                body.append(f'Children of {parents}:')
                for k, (c, info, birth) in enumerate(kids, 1):
                    tally.see(c)
                    again = listed_in.get(c) not in (None, number_of[pid])
                    body.append(_reg_child_line(
                        rp, fn, c, info, birth, _roman_lower(k),
                        number_of.get(c) if not again else None,
                        listed_in[c] if again else None))
                body.append('')
            if living_n:
                for c in _reg_child_ties(rp, pid):
                    if c not in hidden:
                        tally.see(c)
                body += [f"- {_living_count_phrase(living_n, 'child', 'children')}", '']
    k = len(tally.living)
    summary = (f'This report names {len(tally.named)} {"person" if len(tally.named) == 1 else "people"}'
               f" and shows {shown_depth} generation{'' if shown_depth == 1 else 's'}.")
    if k:
        summary += (f' {k} living relatives are counted but not named.' if k != 1
                    else ' 1 living relative is counted but not named.')
    if cut:
        summary += f' Descendants beyond {generations} generations are not printed.'
    lines = [f'# Register report: descendants of {rp.name(root)}', '', summary, ''] + body
    notes = fn.lines()
    if notes:
        lines += ['## Notes', ''] + notes + ['']
    return lines


def run_register(
    archive_root: Path,
    person_id: str | None,
    generations: int | None = None,
    fmt: str = 'md',
) -> Result:
    """Write the printed register report for one deceased person: the book-style
    list of their descendants. Exit codes: 0 written; 1 person not in the index
    (or written with a file left out because it would not read); 2 refused
    (living, private, or a bad --generations); 3 no usable index."""
    precheck = _format_precheck(fmt, ('md', 'html'))
    if precheck is not None:
        return precheck
    gens = _REG_DEFAULT_GENERATIONS if generations is None else generations
    if not 1 <= gens <= _REG_MAX_GENERATIONS:
        print(f'--generations must be between 1 and {_REG_MAX_GENERATIONS} '
              f'(you gave {gens}). Nothing was written.', file=sys.stderr)
        return _views_result(EXIT_ERRORS)
    conn = _open_index_or_explain(archive_root)
    if conn is None:
        return _views_result(EXIT_FAILURE)
    try:
        rp = _ReportPrivacy(conn, archive_root)
        pid = rp.canon(person_id or '')
        if pid is None:
            print(f'No person {fmt_id_display(person_id or "")} in the index. '
                  f'Run fha find "their name" to look them up by name, or fha index '
                  f'if you just added them.', file=sys.stderr)
            return _views_result(EXIT_WARNINGS)
        shown_id = fmt_id_display(pid)
        state = rp.state(pid)
        if state == _PERSON_RESTRICTED:
            unread = sorted(rp.unreadable_paths(pid))
            if unread:
                print(f'Could not read {", ".join(unread)} to check whether {shown_id} '
                      f'asked to be kept private, so no report about them can be printed. '
                      f'Nothing was written. Repair the file (`fha lint` points at '
                      f'what is wrong), run `fha index`, then try again.', file=sys.stderr)
            else:
                print(f'{shown_id} is marked private (restricted), so it is left out of '
                      f'printed reports. Nothing was written.', file=sys.stderr)
            return _views_result(EXIT_ERRORS)
        if state == _PERSON_LIVING:
            name = rp.lookup(pid)['name']
            they, them = _pronouns(rp.sex(pid))
            has = 'have' if they == 'they' else 'has'
            print(f'{name} is recorded as living, or nobody has recorded yet whether '
                  f'{they} died, so a printed report about {them} would name a living '
                  f'person. Choose a deceased ancestor instead. If {they} {has} died, '
                  f'record it first: fha person set-living {shown_id} false',
                  file=sys.stderr)
            return _views_result(EXIT_ERRORS)
        out = _render_report(archive_root, 'register', pid, _register_lines(rp, pid, gens), fmt)
        warnings = rp.unreadable_warnings()
        for warning in warnings:
            print(warning, file=sys.stderr)
        rel = _rel_display(out, archive_root)
        if fmt == 'html':
            print(f'Wrote {rel}. Open it in a browser and print it, or choose Save as PDF '
                  f'in the print box.')
        else:
            print(f'Wrote {rel}.')
        return _views_result(EXIT_WARNINGS if warnings else EXIT_CLEAN, changed=[str(out)])
    except GeneratedFileRefused as e:
        return _views_result(_refused_exit(e))
    except OSError as e:
        return _views_result(_write_os_error_exit(e))
    except RuntimeError as e:
        return _views_result(_runtime_error_exit(e))
    finally:
        conn.close()


def _cmd_register(args: argparse.Namespace) -> int:
    """argparse → run_register bridge; returns the process exit code."""
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return run_register(
        archive_root,
        getattr(args, 'person_id', None),
        getattr(args, 'generations', None),
        getattr(args, 'format', 'md') or 'md',
    ).exit_code


def _add_register_args(p: argparse.ArgumentParser) -> None:
    p.add_argument('person_id', metavar='p-id', help='A deceased person; everyone descended from them is listed.')
    p.add_argument('--generations', type=int, metavar='N', default=None,
                   help=f'How many generations to print, the person being the first (default {_REG_DEFAULT_GENERATIONS}, most {_REG_MAX_GENERATIONS}).')
    p.add_argument('--format', choices=['md', 'html'], default='md', dest='format',
                   help='md (default) or html: a page to print or save as PDF. Both go under generated/views/.')


_AHN_DEFAULT_GENERATIONS = 6
_AHN_MAX_GENERATIONS = 12
_AHN_LIVING_LABEL = 'A living relative (not named)'
_AHN_GENERATION_WORDS = {1: 'parents', 2: 'grandparents', 3: 'great-grandparents'}


def _ahn_generation_label(depth: int) -> str:
    """`Generation 3: grandparents`; the subject's own generation has no word."""
    if depth == 0:
        return 'Generation 1'
    if depth in _AHN_GENERATION_WORDS:
        word = _AHN_GENERATION_WORDS[depth]
    else:
        n = depth - 2   # 4 -> 2nd great-grandparents
        suffix = 'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
        word = f'{n}{suffix} great-grandparents'
    return f'Generation {depth + 1}: {word}'


def _ahn_walk(rp: _ReportPrivacy, root: str, generations: int):
    """Number the subject's ancestors the way `build_ahnentafel_map` does
    (father 2n, mother 2n+1, same-sex or unknown pairs by id), but over the
    ties a printed report may walk (`rp.ties`: through a living or unknown
    relative, never through a restricted one or on withheld evidence), so a
    branch resting on a withheld record is not numbered at all and its
    ancestors drop out with it. Returns `(position -> person, position ->
    first position, position -> sources of the tie to its child)`; a person
    met a second time (cousins marrying) is not climbed again."""
    at: dict[int, str] = {1: root}
    placed: dict[str, int] = {root: 1}
    also: dict[int, int] = {}
    edge_sources: dict[int, set] = {}
    queue = deque([(root, 1)])
    while queue:
        pid, n = queue.popleft()
        if ahnentafel_generation(n) + 1 >= generations:
            continue
        ties = rp.ties(pid, 'parent')
        parents = [(p, rp.sex(p) or 'U') for p, info in sorted(ties.items())
                   if any(is_genetic_parent_subtype(s) for s in info['subtypes'])]
        if not parents:
            continue
        if len(parents) == 1:
            slots = [(parents[0][0], 2 * n if parents[0][1] != 'F' else 2 * n + 1)]
        else:
            father = min(parents, key=lambda e: ({'M': 0, 'U': 1, 'F': 2}.get(e[1], 1), e[0]))[0]
            rest = [e for e in parents if e[0] != father]
            mother = min(rest, key=lambda e: ({'F': 0, 'U': 1, 'M': 2}.get(e[1], 1), e[0]))[0]
            slots = [(father, 2 * n), (mother, 2 * n + 1)]
        for p, pos in slots:
            at[pos] = p
            edge_sources[pos] = set(ties[p]['sources'])
            if p in placed:
                also[pos] = placed[p]
            else:
                placed[p] = pos
                queue.append((p, pos))
    return at, also, edge_sources


def _ahn_no_parents(rp: _ReportPrivacy, root: str) -> str:
    """Why nobody is numbered above no. 1, for the refusal he sees (never
    the printed page, which must not hint at a withheld or adoptive parent):
    an Ahnentafel follows birth parents only, so a person whose only parents
    on record are adoptive or step parents is told that, not that everyone
    is living or private."""
    why = ('This report has no birth parent it can show for this person, so nobody '
           'is numbered above no. 1.')
    if rp.ties(root, 'parent'):
        why += (' This report follows birth parents only; adoptive, step and other '
                'parents are not numbered.')
    return why


def _ahn_lines(rp: _ReportPrivacy, root: str,
               generations: int) -> tuple[list[str], bool, str]:
    """The report as markdown, whether anyone in it is shown by name, and
    (when nobody is numbered above no. 1 and more than one generation was
    asked for) the sentence saying why, for a refusal only."""
    at, also, edge_sources = _ahn_walk(rp, root, generations)
    no_parents = _ahn_no_parents(rp, root) if len(at) == 1 and generations > 1 else ''
    fn = _Footnotes(rp)
    tally = _SheetTally(rp)
    body: list[str] = []
    married: set[frozenset] = set()   # couples whose marriage is printed
    depth_seen = -1
    for pos in sorted(at):
        pid = at[pos]
        state = tally.see(pid)
        depth = ahnentafel_generation(pos)
        if depth != depth_seen:
            body += [f'## {_ahn_generation_label(depth)}', '']
            depth_seen = depth
        name = rp.name(pid) if state == _PERSON_SHOWN else _AHN_LIVING_LABEL
        # Bold, not a markdown list: a preview would renumber a list with
        # gaps (no. 4, 6, 7 shown as 1, 2, 3), and the numbers are the point.
        line = f'**{pos}.** {name}'
        if pos in also:
            line += f' (also no. {also[pos]})'
        line += _cite_all(fn, edge_sources.get(pos, ()))
        body.append(line)
        if state == _PERSON_SHOWN:
            for word, ctype in (() if pos in also else _VITAL_ROWS):
                claim = _report_pick_vital(rp, pid, ctype)
                fact = _fact_line(rp, fn, word, claim) if claim else None
                if fact:
                    body.append(f'- {fact}')
            # A couple's marriage prints once, beside the husband (the even
            # number); the subject, who has no partner on the list, gets the
            # marriages of his own spouses. A man met again at a second number
            # beside a second wife gets that marriage there.
            if pos == 1:
                # In the order they married (undated last, then id), as on
                # the family sheet.
                spouse_ties = rp.ties(pid, 'spouse')
                partners = sorted(spouse_ties, key=lambda s: (
                    spouse_ties[s]['date'] is None, spouse_ties[s]['date'] or '', s))
            elif pos % 2 == 0 and at.get(pos + 1) not in (None, pid):
                partners = [at[pos + 1]]
            else:
                partners = []
            for sp in partners:
                if frozenset((pid, sp)) in married:
                    continue
                marriage = _report_marriage(rp, pid, sp)
                text = _fact_text(rp, fn, marriage) if marriage else ''
                if text and tally.see(sp) == _PERSON_SHOWN:
                    body.append(f'- Married: {rp.name(sp)}, {text}')
                    married.add(frozenset((pid, sp)))
        body.append('')
    shown = bool(tally.named)
    if rp.state(root) == _PERSON_SHOWN:
        title = f'Ancestors of {rp.name(root)}'
    else:
        title = 'Ancestors of a living relative'
    k = len(tally.living)
    summary = (f'This report names {len(tally.named)} '
               f'{"person" if len(tally.named) == 1 else "people"}.')
    if k:
        summary += (f' {k} living relatives are counted but not named.' if k != 1
                    else ' 1 living relative is counted but not named.')
    notes = fn.lines()
    if notes:
        body += ['## Notes', ''] + notes + ['']
    return [f'# {title}', '', summary, ''] + body, shown, no_parents


def run_ahnentafel(
    archive_root: Path,
    person_id: str | None,
    generations: int = _AHN_DEFAULT_GENERATIONS,
    fmt: str = 'md',
) -> Result:
    """Write the printed Ahnentafel report: a numbered list of one person's
    ancestors (1 him, 2 his father, 3 his mother, 4 his father's father...),
    each fact with a note. A living subject is allowed and printed as "a
    living relative"; the deceased ancestors still print. Exit codes: 0
    written; 1 person not in the index (or written with a file left out
    because it would not read); 2 refused (restricted subject, nobody to
    name, or a bad generation count); 3 no usable index."""
    precheck = _format_precheck(fmt, ('md', 'html'))
    if precheck is not None:
        return precheck
    if generations < 1:
        print('--generations must be 1 or more.', file=sys.stderr)
        return _views_result(EXIT_ERRORS)
    if generations > _AHN_MAX_GENERATIONS:
        print(f'Showing {_AHN_MAX_GENERATIONS} generations, the most a printed report goes to.')
        generations = _AHN_MAX_GENERATIONS
    conn = _open_index_or_explain(archive_root)
    if conn is None:
        return _views_result(EXIT_FAILURE)
    try:
        rp = _ReportPrivacy(conn, archive_root)
        pid = rp.canon(person_id or '')
        if pid is None:
            print(f'No person {fmt_id_display(person_id or "")} in the index. '
                  f'Run fha find "their name" to look them up by name, or fha index '
                  f'if you just added them.', file=sys.stderr)
            return _views_result(EXIT_WARNINGS)
        if rp.state(pid) == _PERSON_RESTRICTED:
            unread = sorted(rp.unreadable_paths(pid))
            if unread:
                print(f'Could not read {", ".join(unread)} to check whether '
                      f'{fmt_id_display(pid)} asked to be kept private, so no report about '
                      f'them can be printed. Nothing was written. Repair the file '
                      f'(`fha lint` points at what is wrong), run `fha index`, then try again.',
                      file=sys.stderr)
            else:
                print(f'{fmt_id_display(pid)} is marked private (restricted), so it is left out '
                      f'of printed reports. Nothing was written.', file=sys.stderr)
            return _views_result(EXIT_ERRORS)
        lines, shown, no_parents = _ahn_lines(rp, pid, generations)
        if not shown:
            # A file that would not read may be what cut off the only
            # deceased branch: name it and the fix before refusing.
            for warning in rp.unreadable_warnings():
                print(warning, file=sys.stderr)
            if no_parents:
                print(f'{no_parents} There is nothing to print.', file=sys.stderr)
            else:
                print('Everyone this report would list is living or private, so there is '
                      'nothing to print.', file=sys.stderr)
            return _views_result(EXIT_ERRORS)
        out = _render_report(archive_root, 'ahnentafel', pid, lines, fmt)
        warnings = rp.unreadable_warnings()
        for warning in warnings:
            print(warning, file=sys.stderr)
        rel = _rel_display(out, archive_root)
        if fmt == 'html':
            print(f'Wrote {rel}. Open it in a browser and print it, or choose Save as PDF '
                  f'in the print box.')
        else:
            print(f'Wrote {rel}.')
        return _views_result(EXIT_WARNINGS if warnings else EXIT_CLEAN, changed=[str(out)])
    except GeneratedFileRefused as e:
        return _views_result(_refused_exit(e))
    except OSError as e:
        return _views_result(_write_os_error_exit(e))
    except RuntimeError as e:
        return _views_result(_runtime_error_exit(e))
    finally:
        conn.close()


def _cmd_ahnentafel(args: argparse.Namespace) -> int:
    """argparse → run_ahnentafel bridge; returns the process exit code."""
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    gens = getattr(args, 'generations', None)
    return run_ahnentafel(
        archive_root,
        getattr(args, 'person_id', None),
        _AHN_DEFAULT_GENERATIONS if gens is None else gens,
        getattr(args, 'format', 'md') or 'md',
    ).exit_code


def _cmd_family_sheet(args: argparse.Namespace) -> int:
    """argparse → run_family_sheet bridge; returns the process exit code."""
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return run_family_sheet(
        archive_root,
        getattr(args, 'person_id', None),
        getattr(args, 'spouse', None),
        getattr(args, 'format', 'md') or 'md',
    ).exit_code


# ── Parser registration ───────────────────────────────────────────────────────

# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Build always-current summary pages from your records.

  fha views timeline <p-id>        A person's life chronology
  fha views sources-index <p-id>   Every source about a person
  fha views summary <p-id>         Born / Died / Married / Parents / Children
                                   lines under the person's name
  fha views draft-queue <p-id>     Accepted facts not yet written into the bio
  fha views places                 One note per place, for Obsidian
                                   (only with links: obsidian in fha.yaml)
  fha views refresh                Rebuild every view for all curated people
  fha views family-sheet <p-id>    A printable family group sheet for a couple
  fha views register <p-id>        A printable book-style register of a person's descendants
  fha views ahnentafel <p-id>      A printable numbered list of a person's ancestors

Add --format html (refresh also takes --format both) for a printable
single-file page under generated/views/ (private and unredacted, with a
visible not-for-publication banner); share through `fha site` instead.
The family sheet is the exception: it is made to hand to relatives, so
living relatives are counted but never named and private records are
left out.

These pages are generated and rebuildable; edit the records, not the pages."""


# Shared --format help text for the three content views (refresh differs: it
# also takes 'both').
_FORMAT_HELP = (
    'md (default): the companion .md beside the profile. html: a standalone '
    'printable page under generated/views/ (private and unredacted; it '
    'carries a visible not-for-publication banner; share through `fha site`).'
)


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the `views` subcommand group on the given subparsers action."""
    views_p = subs.add_parser(
        'views',
        help='Generate view files from the index (timeline, sources-index, draft-queue, …).',
        description=_CLI_DESCRIPTION,
    )
    views_p.add_argument('--root', dest='views_root', metavar='PATH',
                         help='Archive root (auto-detected if omitted).')
    views_p.set_defaults(func=_cmd_views_help)

    vsubs = views_p.add_subparsers(dest='views_command', metavar='SUBCOMMAND')

    # ── timeline ──────────────────────────────────────────────────────────────
    tl = vsubs.add_parser(
        'timeline',
        help='Generate per-person timeline view.',
        description=(
            'Generate {name}_timeline_{p-id}.md in the person\'s folder.\n'
            'Requires a fresh .cache/index.sqlite (run `fha index` first).'
        ),
    )
    tl.add_argument('person_id', nargs='?', metavar='p-id',
                    help='Person to generate for.')
    tl.add_argument('--all-curated', action='store_true',
                    help='Generate for every curated person.')
    tl.add_argument('--format', choices=['md', 'html'], default='md', dest='format',
                    help=_FORMAT_HELP)
    tl.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    tl.set_defaults(func=_cmd_timeline)

    # ── sources-index ─────────────────────────────────────────────────────────
    si = vsubs.add_parser(
        'sources-index',
        help="Refresh a person's own ## Sources section.",
        description=(
            'Rewrite the ## Sources GENERATED region inside a person\'s own\n'
            'profile (works on a stub too, so it can be populated before\n'
            'promotion). There is no separate sources-index file and no\n'
            '--format html page; the list lives inside the profile.\n'
            'Requires a fresh .cache/index.sqlite (run `fha index` first).'
        ),
    )
    si.add_argument('person_id', nargs='?', metavar='p-id',
                    help="Person whose ## Sources section to refresh (stub or curated).")
    si.add_argument('--all-curated', action='store_true',
                    help='Refresh every curated profile\'s ## Sources section.')
    si.add_argument('--format', choices=['md', 'html'], default='md', dest='format',
                    help='md (default). html is refused: the list lives inside '
                         'the profile, with no separate page.')
    si.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    si.set_defaults(func=_cmd_sources_index)

    # ── summary ───────────────────────────────────────────────────────────────
    sm = vsubs.add_parser(
        'summary',
        help="Write the Born / Died / Married / Parents / Children lines under a person's name.",
        description=(
            "Write the summary lines under the person's name in their own\n"
            'profile, from accepted claims, between GENERATED summary markers.\n'
            'Lines a person wrote by hand (no markers) are left alone. Works\n'
            'on a stub too. With `views: completeness_line: true` in\n'
            'fha.yaml, the lines end with a Records line saying how well\n'
            'documented the person is, as `fha metrics` says it. Requires a\n'
            'fresh .cache/index.sqlite (run `fha index` first).'
        ),
    )
    sm.add_argument('person_id', nargs='?', metavar='p-id',
                    help='Person whose summary lines to write (stub or curated).')
    sm.add_argument('--all-curated', action='store_true',
                    help="Write every curated profile's summary lines.")
    sm.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    sm.set_defaults(func=_cmd_summary)

    # ── draft-queue ───────────────────────────────────────────────────────────
    dq = vsubs.add_parser(
        'draft-queue',
        help='Generate per-person draft-queue (uncited-claim backlog) view.',
        description=(
            'Generate {name}_draft-queue_{p-id}.md in the person\'s folder.\n'
            'Requires a fresh .cache/index.sqlite (run `fha index` first).'
        ),
    )
    dq.add_argument('person_id', nargs='?', metavar='p-id',
                    help='Person to generate for.')
    dq.add_argument('--all-curated', action='store_true',
                    help='Generate for every curated person.')
    dq.add_argument('--format', choices=['md', 'html'], default='md', dest='format',
                    help=_FORMAT_HELP)
    dq.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    dq.set_defaults(func=_cmd_draft_queue)

    # ── places ────────────────────────────────────────────────────────────────
    pl = vsubs.add_parser(
        'places',
        help='Write one note per place in places/, for Obsidian (links: obsidian only).',
        description=(
            'With links: obsidian in fha.yaml, write one generated note per place\n'
            'in places/ (places/{name}_{l-id}.md), built from places/places.yaml,\n'
            'so Obsidian can follow a place link and show what mentions the place.\n'
            'A place renamed in places.yaml gets a note under its new name, the old\n'
            'note is removed, and links to the old name are pointed at the new one.\n'
            'places.yaml stays the record; edit the place there, never the note.\n'
            'With links: id (the default) nothing is written.\n'
            'Requires a fresh .cache/index.sqlite (run `fha index` first).'
        ),
    )
    pl.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='Show what would be written, removed and re-pointed, '
                         'without writing.')
    pl.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    pl.set_defaults(func=_cmd_places)

    # ── brackets ──────────────────────────────────────────────────────────────
    br = vsubs.add_parser(
        'brackets',
        help='Check couple-folder bracket lists, Ahnentafel placement, and '
             'direct-line stubs (W103/W110/W119/W120).',
    )
    br.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    br.add_argument('--fix', action='store_true', help='Apply renames/moves after preview.')
    br.add_argument('--fix-promote', action='store_true', dest='fix_promote',
                    help='Promote the direct-line stubs (W119) after a preview '
                         'and an Apply? confirmation: tier flip, move into the '
                         'couple folder, research file scaffold.')
    br.add_argument('--realign', action='store_true',
                    help='Bring the whole promoted tree back in line with the '
                         'current derivation in one previewed pass: the '
                         'renames/moves AND the stub promotions together, one '
                         'combined preview, one Apply? confirmation. Use after '
                         'anything that shifts the derived tree (a re-anchored '
                         'root_person, a corrected sex:, a new parent claim, a '
                         'merge).')
    br.add_argument('--generations', type=int, metavar='N',
                    help='Only consider direct-line stubs within N generations '
                         '(1 = parents, 2 = grandparents, …).')
    br.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='Preview changes without writing.')
    br.add_argument('--yes', action='store_true',
                    help='Skip the Apply? confirmation (for scripted or '
                         'non-interactive runs; the preview still prints).')
    br.set_defaults(func=_cmd_brackets)

    # ── tree ──────────────────────────────────────────────────────────────────
    tr = vsubs.add_parser(
        'tree',
        help='Traverse relationships and emit an ancestor/descendant/fan tree.',
        description=(
            'Traverse the relationships table from a seed person and emit the\n'
            'result as neutral JSON (TOOLING §7) or GraphViz DOT.\n'
            'Requires a fresh .cache/index.sqlite (run `fha index` first).'
        ),
    )
    tr.add_argument('person_id', metavar='p-id', help='Seed person for traversal.')
    tr.add_argument(
        '--mode', choices=['ancestors', 'descendants', 'fan'], required=True,
        help='ancestors: pedigree BFS; descendants: all descendants + in-law spouses; '
             'fan: all edge types (default 2 hops).',
    )
    tr.add_argument(
        '--generations', type=int, metavar='N',
        help='Maximum generations / hops (default: unlimited; fan default: 2).',
    )
    tr.add_argument(
        # 'html' is accepted so run_tree can refuse it with a plain pointer
        # (the HTML tree arrives with the site's full-tree feature) instead of
        # a bare argparse choices error.
        '--format', choices=['json', 'dot', 'html'], default='json', dest='format',
        help='Output format (default: json). html is not available here; run '
             '`fha site` for a browsable tree.',
    )
    tr.add_argument('--out', metavar='FILE', help='Write output to FILE instead of stdout.')
    tr.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    tr.set_defaults(func=_cmd_tree)

    # ── family-sheet ──────────────────────────────────────────────────────────
    fs = vsubs.add_parser(
        'family-sheet',
        help='Print a family group sheet: a couple, their parents and children, each fact with its source.',
        description=(
            'Write a printed family group sheet for one deceased person and a spouse\n'
            '(one sheet per spouse when none is named) to generated/views/. Every\n'
            'fact carries a note number that points to its record. Living relatives\n'
            'are counted but never named; private records are left out.\n'
            'Requires a fresh .cache/index.sqlite (run `fha index` first).'
        ),
    )
    _add_family_sheet_args(fs)
    fs.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    fs.set_defaults(func=_cmd_family_sheet)

    # ── register ──────────────────────────────────────────────────────────────
    rg = vsubs.add_parser(
        'register',
        help="Print a book-style register of a person's descendants, each fact with its source.",
        description=(
            "Write a printed NGSQ register report to generated/views/: the person is\n"
            "1, each descendant who has children gets the next number (generation by\n"
            "generation) and a short paragraph, and children are listed with Roman\n"
            "numerals. Living descendants are counted under their parent, never\n"
            "named, and their own children are left out; private records are left out.\n"
            'Requires a fresh .cache/index.sqlite (run `fha index` first).'
        ),
    )
    _add_register_args(rg)
    rg.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    rg.set_defaults(func=_cmd_register)

    # ── ahnentafel ────────────────────────────────────────────────────────────
    ah = vsubs.add_parser(
        'ahnentafel',
        help="Print a numbered list of a person's ancestors, each fact with its source.",
        description=(
            "Write a printed Ahnentafel report to generated/views/: the person is\n"
            "number 1, then 2 his father, 3 his mother, 4 his father's father, and\n"
            "so on, with dates, places and a note for each fact. A living relative\n"
            "is shown as \"A living relative (not named)\"; private records are left\n"
            "out. The numbering starts at 1 for the person asked about, whatever\n"
            "root_generation says.\n"
            'Requires a fresh .cache/index.sqlite (run `fha index` first).'
        ),
    )
    _add_ahnentafel_args(ah)
    ah.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    ah.set_defaults(func=_cmd_ahnentafel)

    # ── clean ─────────────────────────────────────────────────────────────────
    cl = vsubs.add_parser(
        'clean',
        help='Delete every generated view file (companion .md + generated/views/ HTML); '
             'they regenerate with `fha views refresh`.',
        description=(
            'Delete all GENERATED-headed view files: companion .md files (timeline,\n'
            'draft-queue, and any old couple-folder sources-index.md) from the\n'
            'people/ tree, single-file HTML views under generated/views/, and the\n'
            'place notes Obsidian link mode writes in places/. Uses the <!-- GENERATED\n'
            '… --> whole-file header as the sole signal, file by file; never touches\n'
            'profiles or manually authored files, including a profile whose own\n'
            '## Sources section is a GENERATED-BEGIN/END region: that region is\n'
            'rewritten in place by `fha views sources-index`, never deleted by this\n'
            'command, because ownership here is judged by a FILE\'s first line, and a\n'
            "profile's first line is always its frontmatter fence, never a region\n"
            'marker. --dry-run lists what would be removed without writing.'
        ),
    )
    cl.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    cl.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='List what would be removed without writing.')
    cl.set_defaults(func=_cmd_clean)

    # ── refresh ───────────────────────────────────────────────────────────────
    rf = vsubs.add_parser(
        'refresh',
        help='Regenerate all content views for every curated person.',
        description=(
            'Regenerate timeline and draft-queue for every curated person, and\n'
            'refresh every person\'s own ## Sources section and summary lines\n'
            '(stubs included). With links: obsidian in fha.yaml, the place notes\n'
            'are rebuilt first, as `fha views places` does.\n'
            '--format html regenerates the standalone generated/views/ pages\n'
            'instead; --format both writes the two sets in one pass.\n'
            'Requires a fresh .cache/index.sqlite (run `fha index` first).'
        ),
    )
    rf.add_argument('--format', choices=['md', 'html', 'both'], default='md', dest='format',
                    help='md (default): companion .md files as always. html: standalone '
                         'pages under generated/views/. both: write the two sets in one pass.')
    rf.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    rf.set_defaults(func=_cmd_refresh)

    # Store a back-reference so _cmd_views_help can print the right help text
    views_p.set_defaults(_views_parser=views_p)

    return views_p


# ── Standalone entry point ────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha views',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', dest='global_root', metavar='PATH',
                        help='Archive root (auto-detected if omitted).')
    subs = parser.add_subparsers(dest='views_command', metavar='SUBCOMMAND')
    register_standalone(subs)

    args = parser.parse_args(argv)
    if getattr(args, 'root', None) is None:
        args.root = getattr(args, 'global_root', None)
    if not args.views_command:
        parser.print_help()
        return EXIT_CLEAN

    return args.func(args) or EXIT_CLEAN


def register_standalone(subs: argparse._SubParsersAction) -> None:
    """Register subcommands directly (for standalone python tools/views.py invocation)."""
    for name, help_text, func, extra in [
        ('timeline',      'Generate per-person timeline view.',      _cmd_timeline,      _add_person_curated_args),
        ('sources-index', "Refresh a person's ## Sources section.", _cmd_sources_index, _add_si_args),
        ('summary',       "Write a person's summary lines.",        _cmd_summary,       _add_summary_args),
        ('draft-queue',   'Generate per-person draft-queue view.',   _cmd_draft_queue,   _add_person_curated_args),
    ]:
        p = subs.add_parser(name, help=help_text)
        p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
        if extra:
            extra(p)
        p.set_defaults(func=func)

    br = subs.add_parser('brackets', help='Check couple-folder bracket lists, Ahnentafel placement, and direct-line stubs (W103/W110/W119/W120).')
    br.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    br.add_argument('--fix', action='store_true', help='Apply renames/moves after preview.')
    br.add_argument('--realign', action='store_true',
                    help='Renames/moves and stub promotions together - one combined preview, one confirmation.')
    br.add_argument('--fix-promote', action='store_true', dest='fix_promote',
                    help='Promote the direct-line stubs (W119) after a preview and confirmation.')
    br.add_argument('--generations', type=int, metavar='N',
                    help='Only consider direct-line stubs within N generations.')
    br.add_argument('--dry-run', action='store_true', dest='dry_run', help='Preview changes without writing.')
    br.add_argument('--yes', action='store_true', help='Skip the Apply? confirmation (for scripted or non-interactive runs; the preview still prints).')
    br.set_defaults(func=_cmd_brackets)

    cl = subs.add_parser('clean', help='Delete every generated view file (companion .md + generated/views/ HTML); they regenerate with `fha views refresh`.')
    cl.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    cl.add_argument('--dry-run', action='store_true', dest='dry_run', help='List what would be removed without writing.')
    cl.set_defaults(func=_cmd_clean)

    pl = subs.add_parser('places', help='Write one note per place in places/, for Obsidian (links: obsidian only).')
    pl.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='Show what would be written, removed and re-pointed, without writing.')
    pl.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    pl.set_defaults(func=_cmd_places)

    rf = subs.add_parser('refresh', help='Regenerate all content views for every curated person.')
    rf.add_argument('--format', choices=['md', 'html', 'both'], default='md', dest='format')
    rf.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    rf.set_defaults(func=_cmd_refresh)

    fs = subs.add_parser('family-sheet', help='Print a family group sheet: a couple, their parents and children, each fact with its source.')
    _add_family_sheet_args(fs)
    fs.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    fs.set_defaults(func=_cmd_family_sheet)

    rg = subs.add_parser('register', help="Print a book-style register of a person's descendants, each fact with its source.")
    _add_register_args(rg)
    rg.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    rg.set_defaults(func=_cmd_register)

    ah = subs.add_parser('ahnentafel', help="Print a numbered list of a person's ancestors, each fact with its source.")
    _add_ahnentafel_args(ah)
    ah.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    ah.set_defaults(func=_cmd_ahnentafel)

    tr = subs.add_parser('tree', help='Traverse relationships and emit an ancestor/descendant/fan tree.')
    tr.add_argument('person_id', metavar='p-id', help='Seed person for traversal.')
    tr.add_argument('--mode', choices=['ancestors', 'descendants', 'fan'], required=True)
    tr.add_argument('--generations', type=int, metavar='N')
    tr.add_argument('--format', choices=['json', 'dot', 'html'], default='json', dest='format')
    tr.add_argument('--out', metavar='FILE')
    tr.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    tr.set_defaults(func=_cmd_tree)


def _add_family_sheet_args(p: argparse.ArgumentParser) -> None:
    p.add_argument('person_id', metavar='p-id', help='A deceased person; the sheet is for them and a spouse.')
    p.add_argument('--spouse', metavar='p-id', help='Only the sheet for this spouse (default: one sheet per spouse).')
    p.add_argument('--format', choices=['md', 'html'], default='md', dest='format',
                   help='md (default) or html: a page to print or save as PDF. Both go under generated/views/.')


def _add_ahnentafel_args(p: argparse.ArgumentParser) -> None:
    p.add_argument('person_id', metavar='p-id', help='The person whose ancestors are listed (a living person is allowed).')
    p.add_argument('--generations', type=int, metavar='N', default=None,
                   help=f'How many generations to print, the person being the first (default {_AHN_DEFAULT_GENERATIONS}, most {_AHN_MAX_GENERATIONS}).')
    p.add_argument('--format', choices=['md', 'html'], default='md', dest='format',
                   help='md (default) or html: a page to print or save as PDF. Both go under generated/views/.')


def _add_person_curated_args(p: argparse.ArgumentParser) -> None:
    p.add_argument('person_id', nargs='?', metavar='p-id')
    p.add_argument('--all-curated', action='store_true')
    p.add_argument('--format', choices=['md', 'html'], default='md', dest='format')


def _add_summary_args(p: argparse.ArgumentParser) -> None:
    p.add_argument('person_id', nargs='?', metavar='p-id')
    p.add_argument('--all-curated', action='store_true')


def _add_si_args(p: argparse.ArgumentParser) -> None:
    p.add_argument('person_id', nargs='?', metavar='p-id')
    p.add_argument('--all-curated', action='store_true')
    p.add_argument('--format', choices=['md', 'html'], default='md', dest='format')


if __name__ == '__main__':
    sys.exit(main())
