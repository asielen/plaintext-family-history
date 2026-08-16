#!/usr/bin/env python3
"""
views.py - fha views: generate view files from the index.

  fha views timeline [P-id | --all-curated] [--format md|html]
  fha views sources-index [P-id | --all-curated | --couple-folders] [--format md|html]
  fha views draft-queue [P-id | --all-curated] [--format md|html]
  fha views brackets [--fix | --fix-promote | --realign] [--generations N] [--dry-run] [--yes]
  fha views tree <P-id> --mode ancestors|descendants|fan [--generations N] [--format json|dot]
  fha views clean [--dry-run]
  fha views refresh [--format md|html|both]

ARCHITECTURE OVERVIEW
---------------------
Three content views (timeline, sources-index, draft-queue) are read-only
projections derived from the index - they add no new facts.  Each follows
the same pipeline:

  1. Open .cache/index.sqlite  (built by `fha index`; views never write it)
  2. Query claims / sources / persons tables
  3. Write one or more GENERATED-headed .md files back into the archive tree

The GENERATED header (see _gen_header) is the contract between views and lint:
lint rule W105 checks that generated companion files carry this exact header
so accidental hand-edits are caught on the next lint run.

Output files are "companion" files - they live alongside the profile they
describe and share its naming prefix:
    hartley__thomas_edward_P-de957bcda1.md          ← profile (hand-edited)
    hartley__thomas_edward_timeline_P-de957bcda1.md ← generated companion
    hartley__thomas_edward_sources-index_P-de957bcda1.md
    hartley__thomas_edward_draft-queue_P-de957bcda1.md

The couple-folder sources-index is the one exception: it has no P-id because
it describes a whole couple folder, not a single person (TOOLING §7).

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
    ahnentafel_generation,   # Ahnentafel position → generation depth (--generations cap),
    archive_title,        # masthead/page title from fha.yaml site.archive_name,
    build_ahnentafel_map,    # index BFS {P-id → position}; shared with fha person promote,
    couple_folder_dirs,   # digit-prefixed dirs under people/ (not stubs/connections),
    couple_folder_for_prefix,   # canonical on-disk couple folder for a number, or None,
    couple_folder_prefix,    # position → the even couple-folder number,
    extract_token_ids,    # all citation-token IDs ([[X-id]] / legacy [X-id]) in text,
    find_person_record_path,   # disk-truth record lookup for --fix-promote applies,
    fmt_id_display,       # uppercase type prefix for output IDs (p-xxx → P-xxx),
    format_bracket_child,    # `Given` or `Given (adopted)` - shared with lint W103,
    is_generated_text,    # GENERATED-header ownership test (first non-blank line),
    is_genetic_parent_subtype,
    load_fha_yaml,
    load_view_css,        # cached design/view.css loader -> (css, warning_or_None),
    nonbirth_bracket_label,  # 'adopted'/'step'/… mark for a non-birth child,
    normalize_id,         # lower-cases IDs for consistent set/dict keying,
    open_index_db,        # open .cache/index.sqlite with freshness check + table probe,
    promote_person_record,   # the ONE promote engine (tier flip + move + research scaffold),
    read_record,          # parses YAML front-matter + body from a .md file,
    render_template,      # load + render a tools/templates/ Jinja2 template (shared),
    pip_command,
    requirements_hint,
    resolve_root_arg,      # --root flag, else find_archive_root(), shared error message,
    spouse_extended_base,    # add-only `+ second spouse` folder-name rule - shared with lint W103,
    write_generated_file,    # marker-guarded write shared with photoindex gallery,
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
#    _profile_path_for            - locate a person's .md profile file
#    _out_path_for                - build companion file path from profile path
#    _format_sid, _place_label    - formatting helpers
#    _curated_person_ids          - list all curated P-ids from index
#    _couple_folders              - identify curated couple folders + their members
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
#    _write_sources_index         - shared writer (person and couple-folder both call this)
#    _generate_sources_index_person        - per-person sources-index
#    _generate_sources_index_couple_folder - couple-folder sources-index.md
#
#  Draft-queue view  (_generate_draft_queue)
#    _generate_draft_queue        - diff accepted sources against cited tokens
#
#  Brackets view  (_cmd_brackets and helpers)
#    _parse_bracket_names         - extract [Name1 + Name2] list from folder name
#    _folder_numeric_prefix       - '040 Thomas …' → 40
#    _given_name                  - 'Thomas Edward Hartley' → 'Thomas' (first word)
#    _couple_folder_dirs          - list digit-prefixed dirs under people/
#    _persons_in_folder           - person_ids whose profile files live in a folder
#    _build_ahnentafel_map        - BFS from root_person → {pid: int position}
#                                    (+ W120 sex-gap collection via _lib)
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
#    _apply_bracket_fixes         - perform renames/moves after confirmation
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


def _write_view_file(out_path: Path, content: str) -> Path:
    """Write a generated view file, refusing to clobber hand-written content.

    Thin wrapper over the shared _lib.write_generated_file guard (the ownership
    rule lives there so views and the photoindex gallery cannot drift): raises
    GeneratedFileRefused if a file already exists at out_path whose first
    non-blank line is not the views GENERATED marker. The narrower views prefix
    keeps another tool's GENERATED file protected from a views overwrite.
    """
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


def _rebase(p: Path, old: Path, new: Path) -> Path:
    """Return p relocated from old to new, or p unchanged if not under old."""
    try:
        return new / p.relative_to(old)
    except ValueError:
        return p


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
        target, display = m.group(1), m.group(2)
        if display:
            return f'<span class="cite" title="{target}">{display}</span>'
        return f'<span class="cite">{target}</span>'

    return _TOKEN_RE.sub(_token, escaped)


def _html_out_path(archive_root: Path, md_out_path: Path, stem: str | None = None) -> Path:
    """Return (and create the folder for) the HTML twin of a .md view path.

    HTML views land under a visible generated/views/ folder, named by the .md
    twin's stem, so the two formats pair by name and the archive record tree
    stays plain text (the .md companions are Obsidian-linkable research
    artifacts; the HTML is a rendering).  `stem` overrides the name for the
    couple-folder sources-index, whose .md stem ('sources-index') repeats in
    every couple folder - the caller prefixes the folder name to keep one
    collision-free file per folder.
    """
    out_dir = archive_root / 'generated' / 'views'
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f'{stem or md_out_path.stem}.html'


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


def _render_view_html(archive_root: Path, subcommand: str, title: str, body_html: str) -> str:
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
    piece of record text they interpolate. Callers reach this only after
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
    in git diffs:

        hartley__thomas_edward_P-de957bcda1.md          ← profile
        hartley__thomas_edward_timeline_P-de957bcda1.md ← this function's output

    The transformation: strip the trailing _{P-id}, append _{kind}_{P-id}.
    We strip first rather than just inserting before the suffix because the
    profile stem may already contain a prior-generation kind token that would
    produce a double-kind name on regeneration.
    """
    stem = profile_path.stem   # e.g. hartley__thomas_edward_P-de957bcda1
    # Strip trailing _{P-id} suffix (Crockford Base32 alphabet, case-insensitive)
    base = re.sub(r'_[PSCLH]-[0-9a-hjkmnp-tv-z]{10}$', '', stem, flags=re.I)
    # Filename convention: P-id uses uppercase type prefix
    return profile_path.parent / f'{base}_{kind}_{fmt_id_display(person_id)}.md'


def _format_sid(source_id: str) -> str:
    """Format a source ID as a citation token: s-xxx -> [[S-xxx]]."""
    return f'[[{fmt_id_display(source_id)}]]'


def _place_label(place_text: str | None, place_id: str | None,
                 conn: sqlite3.Connection) -> str | None:
    """
    Return a place display string for a claim line.

    A generated view is markdown (not the bare claims YAML), so this is one of
    the few surfaces where a claim's place can actually be clickable in Obsidian:
    a registered `place_id` renders as `[[L-…|label]]` (ID load-bearing, the
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


_RESERVED_VIEW_FOLDERS = ('stubs', 'connections')


def _reserved_view_folder(profile_p: Path | None) -> str | None:
    """The reserved non-couple folder a profile sits directly under, else None.

    Companion views live beside curated profiles in couple folders; people/stubs/
    and people/connections/ are the reserved non-couple folders (`_couple_folders`
    excludes both), so a profile parked in either must not get a companion view.
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
            'All curated persons are still under people/stubs/ or people/connections/ - '
            'nothing was generated; promote them into their couple folders first '
            '(`fha person promote <P-id>` per person, or `fha views brackets '
            '--fix-promote` for the whole direct line).',
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

    Companion views (timeline, sources-index, draft-queue) are curated-person
    files (SPEC §16); they are written *beside the profile* (`_out_path_for`), so a
    curated person's views only stay in a maintained location when the profile
    lives in a couple folder. Two states must be refused:
      - a non-curated (stub/connection) person - a companion file is meaningless;
      - a record whose frontmatter was flipped to `tier: curated` but that still
        physically lives in people/stubs/ - writing beside it drops a GENERATED
        file into stubs/, the wrong home for a curated person's views.
    Guarding on tier *and* location keeps the rule enforced here, not remembered by
    every caller. An unknown pid returns False - the generator's own "no profile
    found" warning covers that case.
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
        f"belong to curated people in their couple folder (SPEC §16), so nothing "
        f"was written. To generate one, promote the record first - "
        f"`fha person promote {fmt_id_display(pid)}` does it in one step for a "
        f"direct-line person (tier, couple-folder filing, research file); "
        f"otherwise set `tier: curated` and move it out of people/stubs/ or "
        f"people/connections/ by hand - then re-run.",
        file=sys.stderr,
    )
    return True


def _couple_folders(conn: sqlite3.Connection, archive_root: Path) -> list[tuple[Path, list[str]]]:
    """
    Return [(folder_path, [person_ids])] for each curated couple folder.

    A "couple folder" is any directory directly under people/ that is not
    stubs/ or connections/ and contains at least one curated profile.
    (TOOLING §7: only folders with ≥1 curated person get a sources-index.md.)

    WHY TWO QUERIES: curated tier is the quality gate for deciding WHICH folders
    get a sources-index - we don't want one for every stub-only folder.  But the
    source union itself should cover everyone in the folder, including stub spouses
    and children.  A stub person may hold a unique source (e.g. an obituary for the
    spouse) that the biographer needs to see even though the stub hasn't been
    promoted to curated yet.  Using the curated-only set for the union would silently
    omit those sources.

    WHY OR ON LIKE: SQLite stores paths with the OS path separator.  On Windows the
    index contains backslashes; LIKE 'people/%' would miss them.  We match both to
    keep the function portable across platforms.
    """
    # Step 1: which folders contain at least one curated profile?
    curated_rows = conn.execute(
        """
        SELECT DISTINCT pf.path
        FROM person_files pf
        JOIN persons p ON pf.person_id = p.id
        WHERE pf.kind = 'profile' AND p.tier = 'curated'
          AND (pf.path LIKE 'people/%' OR pf.path LIKE 'people\\%')
        """
    ).fetchall()

    curated_folders: set[str] = set()
    for row in curated_rows:
        parts = Path(row['path']).parts   # ('people', '040 Thomas + Margaret', 'file.md')
        if len(parts) < 3:
            continue
        folder_name = parts[1]
        if folder_name.lower() not in ('stubs', 'connections'):
            curated_folders.add(folder_name)

    # Step 2: for each qualifying folder, collect ALL profiles (curated and stub).
    result = []
    for folder_name in sorted(curated_folders):
        all_rows = conn.execute(
            """
            SELECT pf.person_id
            FROM person_files pf
            WHERE pf.kind = 'profile'
              AND (pf.path LIKE ? OR pf.path LIKE ?)
            """,
            (f'people/{folder_name}/%', f'people\\{folder_name}\\%'),
        ).fetchall()
        person_ids = [r['person_id'] for r in all_rows]
        if person_ids:
            result.append((archive_root / 'people' / folder_name, person_ids))

    return result


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
    its line: '[unconfirmed - parked {date}]' for a needs-review claim (the
    reviewed: date records when the human last looked - the parked state,
    SPEC §8.1), '[low confidence]' for an accepted claim whose evidence
    quality is low (SPEC §8.5 keeps status and confidence orthogonal: an
    accepted low-confidence fact is built on, and flagged as worth
    corroborating when a better record surfaces). Suggested rows get no tag -
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
    if row['status'] == 'needs-review':
        line += f' [unconfirmed - parked {row["reviewed"]}]' if row['reviewed'] else ' [unconfirmed]'
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
      - One ### Decade section per decade of accepted/needs-review claims
      - Undated claims appear at the bottom of the dated section (ORDER BY puts
        NULL date_min last)
      - ## Unreviewed section at the end for suggested claims
        (suggested = AI-drafted, not yet accepted by a human reviewer)

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

    # date_min is an ISO lower-bound used for sort order only; nulls sort last.
    # We fetch date_edtf separately for display and decade grouping (see _decade_from_edtf).
    main_rows = conn.execute(
        """
        SELECT DISTINCT c.date_edtf, c.date_min, c.type, c.value,
               c.place_id, c.place_text, c.source_id,
               c.status, c.confidence, c.reviewed
        FROM claim_persons cp
        JOIN claims c ON cp.claim_id = c.id
        WHERE cp.person_id = ? AND c.status IN ('accepted', 'needs-review')
        ORDER BY
            CASE WHEN c.date_min IS NULL OR c.date_min = '' THEN 1 ELSE 0 END,
            c.date_min ASC
        """,
        (person_id,),
    ).fetchall()

    suggested_rows = conn.execute(
        """
        SELECT DISTINCT c.date_edtf, c.date_min, c.type, c.value,
               c.place_id, c.place_text, c.source_id,
               c.status, c.confidence, c.reviewed
        FROM claim_persons cp
        JOIN claims c ON cp.claim_id = c.id
        WHERE cp.person_id = ? AND c.status = 'suggested'
        ORDER BY
            CASE WHEN c.date_min IS NULL OR c.date_min = '' THEN 1 ELSE 0 END,
            c.date_min ASC
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
        body = _timeline_body_html(conn, sections, suggested_rows)
        return _write_view_file(
            _html_out_path(archive_root, out_path),
            _render_view_html(
                archive_root, 'timeline', f'Timeline: {person_name}', body),
        )

    parts: list[str] = [_gen_header('timeline'), f'# Timeline: {person_name}\n']

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

    return _write_view_file(out_path, ''.join(parts))


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


def _write_sources_index(
    conn: sqlite3.Connection,
    source_ids: list[str],
    out_path: Path,
    title: str,
    subcommand: str = 'sources-index',
    fmt: str = 'md',
    archive_root: Path | None = None,
    html_stem: str | None = None,
) -> Path:
    """
    Write a sources-index file for the given source IDs; return the path written.

    Shared by both the per-person and couple-folder generators so the output
    format stays identical regardless of scope.  Callers supply the title and
    output path; everything else is derived from the index.
    Sources are grouped by source_type (census, newspaper, other, …) and sorted
    alphabetically within each group.

    One query, two serializers: `fmt='html'` renders the SAME grouped rows as a
    ledger table in the standalone page shell (D11) - `archive_root` is
    required then, and `html_stem` overrides the output stem for the
    couple-folder index (whose .md stem repeats in every folder).
    """
    if fmt == 'html':
        html_path = _html_out_path(archive_root, out_path, stem=html_stem)

    if not source_ids:
        if fmt == 'html':
            return _write_view_file(
                html_path,
                _render_view_html(
                    archive_root, subcommand, title,
                    '<p class="empty">(No sources found.)</p>\n'),
            )
        _write_view_file(
            out_path,
            _gen_header(subcommand) + f'# {title}\n\n*(No sources found.)*\n',
        )
        return out_path

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

    # Group by source_type
    by_type: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        st = row['source_type'] or 'other'
        by_type.setdefault(st, []).append(row)

    if fmt == 'html':
        parts: list[str] = []
        for source_type in sorted(by_type.keys()):
            parts.append(f'<h2>{html_escape(source_type)}</h2>')
            parts.append('<table class="claims">')
            parts.append('<thead><tr><th>Source</th><th>ID</th><th>File</th></tr></thead>')
            parts.append('<tbody>')
            for row in by_type[source_type]:
                sid_html = _md_inline_to_html(_format_sid(row['id']))
                path_text = row['path'].replace('\\', '/')
                parts.append(
                    f'<tr><td><strong>{html_escape(row["title"])}</strong></td>'
                    f'<td>{sid_html}</td>'
                    f'<td class="path">{html_escape(path_text)}</td></tr>'
                )
            parts.append('</tbody></table>')
        return _write_view_file(
            html_path,
            _render_view_html(archive_root, subcommand, title, '\n'.join(parts) + '\n'),
        )

    lines: list[str] = [_gen_header(subcommand), f'# {title}\n']

    for source_type in sorted(by_type.keys()):
        lines.append(f'\n## {source_type}\n')
        for row in by_type[source_type]:
            sid_token = _format_sid(row['id'])
            path_text = row['path'].replace('\\', '/')
            lines.append(f'\n**{row["title"]}** {sid_token}  \n')
            lines.append(f'  {path_text}\n')

    _write_view_file(out_path, ''.join(lines))
    return out_path


def _generate_sources_index_person(
    conn: sqlite3.Connection, person_id: str, archive_root: Path, fmt: str = 'md'
) -> Path | None:
    """Generate the per-person sources-index. Returns the path written, or None."""
    profile_p = _profile_path_for(conn, person_id, archive_root)
    if not profile_p:
        print(f'WARNING: no profile found for {person_id} - skipped.', file=sys.stderr)
        return None

    name_row = conn.execute('SELECT name FROM persons WHERE id = ?', (person_id,)).fetchone()
    person_name = name_row['name'] if name_row else person_id

    source_ids = _source_ids_for_persons(conn, [person_id])
    out_path = _out_path_for(profile_p, 'sources-index', person_id)
    return _write_sources_index(
        conn, source_ids, out_path, f'Sources: {person_name}',
        fmt=fmt, archive_root=archive_root,
    )


def _generate_sources_index_couple_folder(
    conn: sqlite3.Connection,
    folder_path: Path,
    person_ids: list[str],
    fmt: str = 'md',
    archive_root: Path | None = None,
) -> Path:
    """
    Generate the couple-folder sources-index.md at the folder root (or, for
    fmt='html', the standalone twin `generated/views/{folder}_sources-index.html`
    - the folder name is prefixed because every couple folder shares the same
    .md stem).  Returns the path written.
    """
    source_ids = _source_ids_for_persons(conn, person_ids)
    out_path = folder_path / 'sources-index.md'
    folder_name = folder_path.name
    return _write_sources_index(
        conn, source_ids, out_path, f'Sources: {folder_name}',
        fmt=fmt, archive_root=archive_root,
        html_stem=f'{folder_name}_sources-index',
    )


# ── Draft-queue ───────────────────────────────────────────────────────────────

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
    matter) for citation tokens via extract_token_ids (new `[[S-…]]` or legacy
    `[S-…]`).  The body is where the summary block and biography prose live.  If
    an S-id token appears anywhere in the body, that source is considered cited
    and omitted from the queue.

    HOW THE DIFF WORKS:
      accepted_sids  = {source_ids with ≥1 accepted claim for this person}
      cited_sids     = {S-ids found as [S-xxx] tokens in the profile body}
      uncited_sids   = accepted_sids - cited_sids   ← the queue

    The queue lists each uncited source with its accepted claims so the
    biographer can see what facts still need to be woven into the prose.
    Returns the path written, or None if the person has no profile in the index.
    """
    profile_p = _profile_path_for(conn, person_id, archive_root)
    if not profile_p:
        print(f'WARNING: no profile found for {person_id} - skipped.', file=sys.stderr)
        return None

    name_row = conn.execute('SELECT name FROM persons WHERE id = ?', (person_id,)).fetchone()
    person_name = name_row['name'] if name_row else person_id

    rec = read_record(profile_p)
    body = rec['body']
    # Citation tokens (new `[[S-…]]` or legacy `[S-…]`); filter to S- sources.
    cited_sids: set[str] = {
        tid for tid in extract_token_ids(body) if tid.startswith('s-')
    }

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

    lines: list[str] = [_gen_header('draft-queue'), f'# Draft Queue: {person_name}\n']

    if not uncited_sids:
        if fmt == 'html':
            return _write_view_file(
                _html_out_path(archive_root, out_path),
                _render_view_html(
                    archive_root, 'draft-queue', f'Draft Queue: {person_name}',
                    '<p>All accepted claims are cited in the profile.</p>\n'),
            )
        lines.append('\nAll accepted claims are cited in the profile.\n')
        return _write_view_file(out_path, ''.join(lines))

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
            f'<p>{len(uncited_sids)} source(s) with accepted claims '
            f'not yet cited in the profile:</p>'
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

    return _write_view_file(out_path, ''.join(lines))


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


def _given_name(full_name: str) -> str:
    """Return the first word of a full name - the form used in bracket lists."""
    parts = full_name.strip().split()
    return parts[0] if parts else full_name


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
) -> dict[str, int]:
    """BFS from root_pid to build {person_id → Ahnentafel position}.

    Delegates to `_lib.build_ahnentafel_map` - the derivation moved there so
    `fha person promote` and `fha report` §7b can compute the same positions
    without importing views (tools never import tools). Behavior is identical;
    the docstring and algorithm live with the shared function. `sex_gaps`
    collects the W120 set (single-parent placements defaulted for lack of a
    recorded `sex:`) when a list is passed.
    """
    return build_ahnentafel_map(conn, root_pid, sex_gaps)


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
        derived_entries = []
        for info in child_info.values():
            natures = info['natures']
            label = None
            if not any(is_genetic_parent_subtype(s) for s in natures):
                for s in sorted(natures):
                    label = nonbirth_bracket_label(s)
                    if label:
                        break
            derived_entries.append(format_bracket_child(_given_name(info['name']), label))
        derived_names = sorted(derived_entries)

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
    timeline, sources-index, and draft-queue files regardless of whether they
    are in the SQLite index (generated files carry no frontmatter id and are
    therefore absent from person_files).

    WHY DISK SCAN: generated view files (timeline, sources-index, draft-queue)
    lack a frontmatter `id:` field so index.py does not add them to
    person_files.  Querying person_files alone would leave them behind when a
    W110 file-move fix is applied.  Scanning disk is the only reliable way to
    move all of a person's companion files atomically.
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

        # Scan ALL couple dirs for companion files that belong to this person but
        # are not in the expected folder - catches strays even when the profile
        # itself is already correctly placed.
        for folder in all_couple_dirs:
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
                issues.append({
                    'code': 'W110',
                    'kind': 'file_move',
                    'src_path': src_path,
                    'dst_path': dst_path,
                    'expected_folder': dest_folder,
                    'msg': (
                        f'W110 people/{folder.name}/{src_path.name}: '
                        f'{person_name} (Ahnentafel {pos}) '
                        f'is in folder prefix {actual_prefix}, '
                        f'expected {expected_prefix}'
                    ),
                })

    return issues


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
        namer = min(members, key=lambda c: c['pos'])
        partner = next(
            (c for c in members if c['pos'] == namer['pos'] + 1), None)
        folder_label = namer['name']
        if partner is not None and partner['name']:
            folder_label = f'{namer["name"]} + {partner["name"]}'
        shared_dest[prefix] = (
            archive_root / 'people' / f'{str(prefix).zfill(3)} {folder_label}')

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
    post_folders = [rename_map.get(f, f) for f in _couple_folder_dirs(archive_root)]
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
            namer = min(members, key=lambda c: c['pos'])
            partner = next(
                (c for c in members if c['pos'] == namer['pos'] + 1), None)
            label = namer['name']
            if partner is not None and partner['name']:
                label = f'{namer["name"]} + {partner["name"]}'
            dest = archive_root / 'people' / f'{str(prefix).zfill(3)} {label}'
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


def _apply_bracket_fixes(
    w103: list[dict], w110: list[dict], archive_root: Path
) -> tuple[int, bool]:
    """Apply renames and moves collected by the check functions.

    A preflight first checks every folder-rename destination. If any already
    exists (and is not its own source), NOTHING is mutated and the function
    returns (conflict_count, aborted=True) so the caller can fail before any
    partial change.

    Otherwise it renames folders (os.rename) and moves person files
    (shutil.move), creating destination folders as needed. Folder-rename
    failures and file-move skips are both counted as `failures`.

    Returns (failures, aborted). Callers treat aborted as EXIT_FAILURE and any
    nonzero `failures` as EXIT_WARNINGS - a non-empty fix run must never report
    clean after a rename/move could not be applied.
    """
    # ── Preflight: folder-rename destination conflicts ────────────────────────
    conflicts: list[Path] = []
    for item in w103:
        dst = item['folder'].parent / item['new_name']
        if dst != item['folder'] and dst.exists():
            conflicts.append(dst)
    for item in w110:
        if item['kind'] != 'folder_rename':
            continue
        if item['new_folder'] != item['old_folder'] and item['new_folder'].exists():
            conflicts.append(item['new_folder'])
    if conflicts:
        for c in conflicts:
            try:
                rel = c.relative_to(archive_root)
            except ValueError:
                rel = c
            print(
                f'  ERROR: rename destination already exists: {rel} '
                '- no changes applied.',
                file=sys.stderr,
            )
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
                         sources-index, draft-queue) to the correct folder.
                         (Requires root_person.)
      4. W119 - direct-line ancestors (derived position >= 2) still filed as
                 stubs (`tier: stub`, or a record under people/stubs/).
                 (Requires root_person.  `generations` caps the depth.)
    Plus one report-only note: W120 - a placement the derivation made by
    default (a lone linked parent with no recorded `sex:` takes the father/even
    slot); nothing mechanical to fix - the human records `sex:` to settle it.

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

        if root_person_raw:
            root_pid = normalize_id(str(root_person_raw))
            if conn.execute('SELECT id FROM persons WHERE id=?', (root_pid,)).fetchone() is None:
                print(
                    f'WARNING: root_person {root_pid!r} is not in the index - '
                    'Ahnentafel checks (W110/W119) skipped. Run `fha index` or fix fha.yaml.',
                    file=sys.stderr,
                )
            else:
                sex_gaps: list[dict] = []
                pid_to_pos = _build_ahnentafel_map(conn, root_pid, sex_gaps)
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
                            f'W120 {where}: {gap_name} took Ahnentafel '
                            f'position {gap["pos"]} (the father/even slot) by '
                            'default - they are the only linked parent of that '
                            'couple and their record has no sex: recorded, so '
                            'this slot and every ancestor number above it is a '
                            'guess, not a derivation. Record `sex: M` or '
                            '`sex: F` on their record to confirm or correct '
                            'it, then re-run `fha views brackets`.'
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

        all_issues = w103 + w110 + w119 + w120
        issue_data = {'w103': len(w103), 'w110': len(w110),
                      'w119': len(w119), 'w120': len(w120)}

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
                # Only report-only notes (W120) remain - nothing mechanical to
                # realign; the note above already names the human's fix.
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
            for item in w110:
                if item['kind'] == 'folder_rename':
                    rename_map[item['old_folder']] = item['new_folder']
            rebased, dest_problem = _realign_promotion_dests(
                w119, rename_map, archive_root)
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
                        f'\nNo changes written - {failures} rename '
                        'destination(s) already exist (see stderr). Resolve '
                        'the conflicts, then re-run.',
                        file=sys.stderr,
                    )
                    return _views_result(EXIT_FAILURE, data=issue_data)

            promoted = 0
            if w119:
                promoted, pfail = _apply_promotions(w119, archive_root)
                failures += pfail

            # Renames, moves, and promotions all change paths without touching
            # mtimes, so the index cannot see it is stale - drop the cache
            # outright, same rationale as the --fix and --fix-promote paths.
            conn.close()
            db_path = archive_root / '.cache' / 'index.sqlite'
            try:
                db_path.unlink(missing_ok=True)
            except OSError as exc:
                reason = exc.strerror or str(exc)
                print(
                    f'\nThe realignment was applied, but the search index '
                    f'cache could not be cleared ({reason}): {db_path}. The '
                    'records moved without changing their file times, so the '
                    'leftover index still points at the old locations and can '
                    'look up to date - searches may quietly go stale until '
                    f'you rebuild it. Delete {db_path} (or just run '
                    '`fha index`) to rebuild the index and clear the stale '
                    'entries.',
                    file=sys.stderr,
                )
                return _views_result(
                    EXIT_WARNINGS,
                    data={**issue_data, 'index_stale': True,
                          'promoted': promoted, 'failures': failures})
            changed = [str(db_path)]

            done_parts = []
            if w103 or w110:
                done_parts.append('renames/moves applied')
            if w119:
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
                    EXIT_WARNINGS if (w103 or w110 or w120) else EXIT_CLEAN,
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
                # mtime-based staleness check cannot see (same rationale as
                # the W110 fix below). Drop the cache to force a rebuild.
                conn.close()
                db_path = archive_root / '.cache' / 'index.sqlite'
                try:
                    db_path.unlink(missing_ok=True)
                except OSError as exc:
                    # The records ALREADY moved on disk - that is done and
                    # irreversible; only the cache drop failed (a locked or
                    # read-only .cache/index.sqlite). A move preserves each
                    # record's mtime, so the undeleted index still passes the
                    # freshness check while pointing at the OLD stubs/ paths -
                    # searches can quietly go stale. Report the promotions that
                    # landed, then warn non-zero naming the stale cache and the
                    # rebuild command; never let the unlink traceback reach the
                    # user (AGENTS_TOOLING: no traceback ever reaches him).
                    reason = exc.strerror or str(exc)
                    print(
                        f'\nPromoted {applied} of {len(w119)} - those records '
                        'moved and are filed under their couple folders. But the '
                        f'search index cache could not be cleared ({reason}): '
                        f'{db_path}. The records moved, so the leftover index '
                        'still points at their old locations and can look up to '
                        'date, which means searches may quietly go stale until '
                        f'you rebuild it. Delete {db_path} (or just run '
                        '`fha index`) to rebuild the index and clear the stale '
                        'entries.',
                        file=sys.stderr,
                    )
                    return _views_result(
                        EXIT_WARNINGS,
                        data={**issue_data, 'index_stale': True,
                              'promoted': applied, 'failures': failures})
                changed = [str(db_path)]
                print(
                    f'\nPromoted {applied} of {len(w119)}. Run `fha index` to '
                    'rebuild the index, then `fha views refresh` to generate '
                    'the new curated people\'s companion views (timeline, '
                    'sources-index, draft-queue).'
                )
            if failures:
                print(f'{failures} promotion(s) not applied - see stderr.')
                return _views_result(EXIT_WARNINGS, changed=changed,
                                     data={**issue_data, 'failures': failures})
            return _views_result(
                EXIT_WARNINGS if (w103 or w110 or w120) else EXIT_CLEAN,
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
            print('\nNothing here is applied by --fix - ' + '; '.join(notes) + '.')
            return _views_result(EXIT_WARNINGS, data=issue_data)

        # ── Preview ───────────────────────────────────────────────────────
        _print_bracket_preview(w103, w110, archive_root)

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
                f'\nNo changes written - {failures} rename destination(s) already '
                'exist (see stderr). Resolve the conflicts, then re-run.',
                file=sys.stderr,
            )
            return _views_result(EXIT_FAILURE, data=issue_data)

        # Renames/moves change person_files.path without touching any file's
        # mtime, so newest_record_mtime() can't detect the index is now stale.
        # Remove the cache outright to force a rebuild before it's next read.
        conn.close()
        db_path = archive_root / '.cache' / 'index.sqlite'
        try:
            db_path.unlink(missing_ok=True)
        except OSError as exc:
            # The renames/moves ALREADY landed on disk and are irreversible;
            # only the cache drop failed (a locked or read-only cache). Those
            # moves keep each file's mtime, so the leftover index looks fresh
            # while pointing at the OLD paths - searches can quietly go stale.
            # Report the work that landed and warn non-zero naming the stale
            # cache and the rebuild command; no traceback reaches the user.
            reason = exc.strerror or str(exc)
            print(
                f'\nThe renames and moves were applied, but the search index '
                f'cache could not be cleared ({reason}): {db_path}. Those moves '
                'change record paths without changing their mtimes, so the '
                'leftover index still points at the old locations and can look '
                'up to date - searches may quietly go stale until you rebuild '
                f'it. Delete {db_path} (or just run `fha index`) to rebuild the '
                'index and clear the stale entries.',
                file=sys.stderr,
            )
            data = {**issue_data, 'index_stale': True}
            if failures:
                data['failures'] = failures
            return _views_result(EXIT_WARNINGS, data=data)
        changed = [str(db_path)]

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
    claim.  At most one accepted claim of each vital type per person is expected,
    so no ordering guarantee is needed.
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
    for row in conn.execute(
        f"""
        SELECT cp.person_id, c.type, c.date_edtf
        FROM claims c JOIN claim_persons cp ON c.id = cp.claim_id
        WHERE cp.person_id IN ({placeholders})
          AND c.type IN ('birth', 'death') AND c.status = 'accepted'
          AND COALESCE(c.negated, 0) = 0
        """,
        pids,
    ).fetchall():
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
        SELECT DISTINCT r.rel, r.other_id, r.claim_id, r.date_start, r.date_end, c.subtype
        FROM relationships r LEFT JOIN claims c ON r.claim_id = c.id
        WHERE r.person_id = ? AND r.rel IN ({placeholders})
        """,
        [pid] + rels,
    ).fetchall()
    return [
        {
            'type': row['rel'],
            'from': pid,
            'to': row['other_id'],
            'claim_id': row['claim_id'],
            # The bond's nature (SPEC §12.2): a non-genetic parent/child edge draws
            # distinctly; unset/legacy subtypes default to genetic, back-compatibly.
            'subtype': (row['subtype'] or '').strip().lower() or None,
            'genetic': is_genetic_parent_subtype(row['subtype']),
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
    relationship types use null per TOOLING §7.  The claim_id and P-ids get
    their type prefix uppercased for output consistency.
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
    """
    precheck = _format_precheck(fmt, ('md', 'html'))
    if precheck is not None:
        return precheck
    conn = open_index_db(archive_root, ('persons',), strict=True)
    if conn is None:
        return _views_result(EXIT_FAILURE)

    changed: list[str] = []
    try:
        if all_curated:
            person_ids = _view_eligible_curated_ids(conn, archive_root)
            if not person_ids:
                return _empty_curated_views_result(conn)
            count = 0
            for pid in person_ids:
                out = _generate_or_warn(
                    f'timeline for {pid}', _generate_timeline, conn, pid, archive_root, fmt=fmt,
                )
                if out:
                    print(f'  timeline ->{out.relative_to(archive_root)}')
                    changed.append(str(out))
                    count += 1
            print(f'Generated {count} timeline file(s).')
            if count and fmt == 'md':
                # Writing a companion file makes the index stale (its mtime now
                # post-dates .cache/index.sqlite), but a successful write is not a
                # warning: exit clean and print the reindex as advice, not an alarm.
                # (HTML lands under generated/, which the indexer never scans.)
                print('Run `fha index` when convenient to update the search index with the new view file(s).')
            return _views_result(EXIT_CLEAN, changed=changed, data={'count': count})

        if not person_id:
            print('ERROR: provide a P-id or --all-curated.', file=sys.stderr)
            return _views_result(EXIT_FAILURE)

        pid = normalize_id(person_id)
        if _skip_stub_person(conn, pid, 'timeline', archive_root):
            return _views_result(EXIT_WARNINGS, data={'count': 0})
        out = _generate_timeline(conn, pid, archive_root, fmt=fmt)
        if out:
            print(f'  timeline ->{out.relative_to(archive_root)}')
            if fmt == 'md':
                print('Run `fha index` when convenient to update the search index with the new view file.')
            changed.append(str(out))
            return _views_result(EXIT_CLEAN, changed=changed, data={'count': 1})
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
    couple_folders_only: bool = False,
    fmt: str = 'md',
) -> Result:
    """Generate sources-index companion/couple-folder file(s); return a Result.

    Progress narration stays inline (byte-identical); written files land in
    `changed`.  `fmt='html'` writes standalone twins under generated/views/
    (couple folders as `{folder}_sources-index.html`); HTML writes never stale
    the index, so the reindex advice stays md-only.
    """
    precheck = _format_precheck(fmt, ('md', 'html'))
    if precheck is not None:
        return precheck
    conn = open_index_db(archive_root, ('persons',), strict=True)
    if conn is None:
        return _views_result(EXIT_FAILURE)

    changed: list[str] = []
    try:
        if all_curated or couple_folders_only:
            count = 0
            if all_curated:
                # Per-person files for all curated persons
                for pid in _view_eligible_curated_ids(conn, archive_root):
                    out = _generate_or_warn(
                        f'sources-index for {pid}', _generate_sources_index_person,
                        conn, pid, archive_root, fmt=fmt,
                    )
                    if out:
                        print(f'  sources-index ->{out.relative_to(archive_root)}')
                        changed.append(str(out))
                        count += 1

            # Couple-folder sources-index.md files
            for folder_path, person_ids in _couple_folders(conn, archive_root):
                out = _generate_or_warn(
                    f'sources-index for {folder_path.name}', _generate_sources_index_couple_folder,
                    conn, folder_path, person_ids, fmt=fmt, archive_root=archive_root,
                )
                if out:
                    print(f'  sources-index ->{out.relative_to(archive_root)}')
                    changed.append(str(out))
                    count += 1

            print(f'Generated {count} sources-index file(s).')
            if count:
                # Writing a companion file makes the index stale (its mtime now
                # post-dates .cache/index.sqlite), but a successful write is not a
                # warning: exit clean and print the reindex as advice, not an alarm.
                # (HTML lands under generated/, which the indexer never scans.)
                if fmt == 'md':
                    print('Run `fha index` when convenient to update the search index with the new view file(s).')
                return _views_result(EXIT_CLEAN, changed=changed, data={'count': count})
            if all_curated and not changed:
                # Nothing generated because every curated record is parked in
                # people/stubs/ (couple_folders_only with 0 folders stays clean).
                return _empty_curated_views_result(conn)
            return _views_result(EXIT_CLEAN, changed=changed, data={'count': count})

        if not person_id:
            print('ERROR: provide a P-id, --all-curated, or --couple-folders.', file=sys.stderr)
            return _views_result(EXIT_FAILURE)

        pid = normalize_id(person_id)
        if _skip_stub_person(conn, pid, 'sources-index', archive_root):
            return _views_result(EXIT_WARNINGS, data={'count': 0})
        out = _generate_sources_index_person(conn, pid, archive_root, fmt=fmt)
        if out:
            print(f'  sources-index ->{out.relative_to(archive_root)}')
            if fmt == 'md':
                print('Run `fha index` when convenient to update the search index with the new view file.')
            changed.append(str(out))
            return _views_result(EXIT_CLEAN, changed=changed, data={'count': 1})
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
        couple_folders_only=getattr(args, 'couple_folders', False),
        fmt=getattr(args, 'format', 'md') or 'md',
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
    """
    precheck = _format_precheck(fmt, ('md', 'html'))
    if precheck is not None:
        return precheck
    conn = open_index_db(archive_root, ('persons',), strict=True)
    if conn is None:
        return _views_result(EXIT_FAILURE)

    changed: list[str] = []
    try:
        if all_curated:
            person_ids = _view_eligible_curated_ids(conn, archive_root)
            if not person_ids:
                return _empty_curated_views_result(conn)
            count = 0
            for pid in person_ids:
                out = _generate_or_warn(
                    f'draft-queue for {pid}', _generate_draft_queue, conn, pid, archive_root, fmt=fmt,
                )
                if out:
                    print(f'  draft-queue ->{out.relative_to(archive_root)}')
                    changed.append(str(out))
                    count += 1
            print(f'Generated {count} draft-queue file(s).')
            if count and fmt == 'md':
                # Writing a companion file makes the index stale (its mtime now
                # post-dates .cache/index.sqlite), but a successful write is not a
                # warning: exit clean and print the reindex as advice, not an alarm.
                # (HTML lands under generated/, which the indexer never scans.)
                print('Run `fha index` when convenient to update the search index with the new view file(s).')
            return _views_result(EXIT_CLEAN, changed=changed, data={'count': count})

        if not person_id:
            print('ERROR: provide a P-id or --all-curated.', file=sys.stderr)
            return _views_result(EXIT_FAILURE)

        pid = normalize_id(person_id)
        if _skip_stub_person(conn, pid, 'draft-queue', archive_root):
            return _views_result(EXIT_WARNINGS, data={'count': 0})
        out = _generate_draft_queue(conn, pid, archive_root, fmt=fmt)
        if out:
            print(f'  draft-queue ->{out.relative_to(archive_root)}')
            if fmt == 'md':
                print('Run `fha index` when convenient to update the search index with the new view file.')
            changed.append(str(out))
            return _views_result(EXIT_CLEAN, changed=changed, data={'count': 1})
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


def run_clean(archive_root: Path, dry_run: bool = False) -> Result:
    """Delete GENERATED view files; return a Result (prints progress inline).

    Two sweeps, one ownership test (the views marker on the first non-blank
    line - marker-per-file, never folder ownership):
      1. companion `.md` files in the people/ tree, as always;
      2. single-file `.html` views (and any stray `.md`) under generated/views/.
    Hand-authored files are never touched in either place.  Removed paths are
    recorded in `changed`; under --dry-run nothing is deleted, both sweeps are
    listed, and `changed` stays empty.

    Exit codes: removing a people/-tree companion leaves stale rows in the
    index (exit 1 + the reindex note, as always); generated/views/ files are
    never indexed, so a sweep that removed only those exits 0 clean.
    """
    dry_run = bool(dry_run)

    people_dir = archive_root / 'people'
    if not people_dir.is_dir():
        print('ERROR: people/ directory not found.', file=sys.stderr)
        return _views_result(EXIT_FAILURE)

    found: list[Path] = []
    for p in sorted(people_dir.rglob('*.md')):
        try:
            text = p.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        # Owned by views only when the marker is the FIRST non-blank line - a
        # hand-written file that merely mentions the marker later is never
        # deleted. Shared _lib predicate; the views-specific prefix keeps
        # other tools' GENERATED files out of this clean.
        if is_generated_text(text, prefix=_GEN_MARKER):
            found.append(p)

    # Second sweep: standalone HTML views under generated/views/ (D11). Same
    # marker-per-file ownership - the folder is visible and a human may park
    # anything there; only files views wrote are removed.
    gen_found: list[Path] = []
    gen_dir = archive_root / 'generated' / 'views'
    if gen_dir.is_dir():
        for p in sorted(list(gen_dir.glob('*.html')) + list(gen_dir.glob('*.md'))):
            try:
                text = p.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            if is_generated_text(text, prefix=_GEN_MARKER):
                gen_found.append(p)

    all_found = found + gen_found
    if not all_found:
        print('No GENERATED view files found.')
        return _views_result(EXIT_CLEAN)

    changed: list[str] = []
    for p in all_found:
        rel = p.relative_to(archive_root)
        if dry_run:
            print(f'  would remove {rel}')
        else:
            p.unlink()
            print(f'  removed {rel}')
            changed.append(str(p))

    verb = 'Would remove' if dry_run else 'Removed'
    print(f'{verb} {len(all_found)} generated file(s).')
    if not dry_run:
        if found:
            print('Note: deleted files still appear in .cache/index.sqlite - run `fha index` to update the cache.')
            return _views_result(EXIT_WARNINGS, changed=changed, data={'removed': len(all_found)})
        # Only generated/views/ files were removed - the index never saw them.
        return _views_result(EXIT_CLEAN, changed=changed, data={'removed': len(all_found)})
    return _views_result(EXIT_CLEAN, data={'removed': 0, 'would_remove': len(all_found)})


def _cmd_clean(args: argparse.Namespace) -> int:
    """argparse → run_clean bridge; returns the process exit code."""
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return run_clean(archive_root, dry_run=getattr(args, 'dry_run', False)).exit_code


def run_refresh(archive_root: Path, fmt: str = 'md') -> Result:
    """Regenerate every curated person's view files; return a Result (prints inline).

    Written files are recorded in `changed`; the per-file progress lines stay
    byte-for-byte as before.

    `fmt` selects the output set: 'md' (default) the companion files as
    always; 'html' the standalone generated/views/ set only; 'both' the two
    sets in one pass (owner decision 7-Q2).  The reindex advice prints only
    when .md companions were written - HTML never stales the index.
    """
    precheck = _format_precheck(fmt, ('md', 'html', 'both'))
    if precheck is not None:
        return precheck
    fmt_passes: tuple[str, ...] = ('md', 'html') if fmt == 'both' else (fmt,)
    conn = open_index_db(archive_root, ('persons',), strict=True)
    if conn is None:
        return _views_result(EXIT_FAILURE)

    changed: list[str] = []
    try:
        person_ids = _view_eligible_curated_ids(conn, archive_root)
        if not person_ids:
            return _empty_curated_views_result(conn)

        _per_person = [
            (_generate_timeline,             'timeline      '),
            (_generate_draft_queue,          'draft-queue   '),
            (_generate_sources_index_person, 'sources-index '),
        ]
        count = 0
        for pid in person_ids:
            for fn, label in _per_person:
                for fmt_pass in fmt_passes:
                    out = _generate_or_warn(
                        f'{label.strip()} for {pid}', fn, conn, pid, archive_root, fmt=fmt_pass,
                    )
                    if out:
                        print(f'  {label}->{out.relative_to(archive_root)}')
                        changed.append(str(out))
                        count += 1

        for folder_path, pids_in_folder in _couple_folders(conn, archive_root):
            for fmt_pass in fmt_passes:
                out = _generate_or_warn(
                    f'sources-index for {folder_path.name}', _generate_sources_index_couple_folder,
                    conn, folder_path, pids_in_folder, fmt=fmt_pass, archive_root=archive_root,
                )
                if out:
                    print(f'  sources-index  ->{out.relative_to(archive_root)}')
                    changed.append(str(out))
                    count += 1

        print(f'Generated {count} view file(s).')
        if count and 'md' in fmt_passes:
            # Refresh writes new/updated companion files, which makes the index
            # stale by definition (newest_record_mtime now post-dates it). That is
            # not a failure of the refresh: exit clean and print the reindex as
            # advice, so a harness following the exit code isn't alarmed each run.
            # (An html-only refresh writes under generated/ and stales nothing.)
            print('Run `fha index` when convenient to update the search index with the new view files.')
        return _views_result(EXIT_CLEAN, changed=changed, data={'count': count})

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
        print('Subcommands: timeline, sources-index, draft-queue, brackets, tree')
    # Bare `fha views` (no verb) is a usage error, not clean success: exit 2,
    # matching `fha person`/`fha confirm`/`fha places` (audit flag 15).
    return EXIT_ERRORS


# ── Parser registration ───────────────────────────────────────────────────────

# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Build always-current summary pages from your records.

  fha views timeline <P-id>        A person's life chronology
  fha views sources-index <P-id>   Every source about a person
  fha views draft-queue <P-id>     Accepted facts not yet written into the bio
  fha views refresh                Rebuild every view for all curated people

Add --format html (refresh also takes --format both) for a printable
single-file page under generated/views/ - private and unredacted, with a
visible not-for-publication banner; share through `fha site` instead.

These pages are generated and rebuildable; edit the records, not the pages."""


# Shared --format help text for the three content views (refresh differs: it
# also takes 'both').
_FORMAT_HELP = (
    'md (default): the companion .md beside the profile. html: a standalone '
    'printable page under generated/views/ (private and unredacted - it '
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
            'Generate {name}_timeline_{P-id}.md in the person\'s folder.\n'
            'Requires a fresh .cache/index.sqlite (run `fha index` first).'
        ),
    )
    tl.add_argument('person_id', nargs='?', metavar='P-id',
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
        help='Generate per-person (and optionally couple-folder) sources-index view.',
        description=(
            'Generate {name}_sources-index_{P-id}.md per person, and/or\n'
            'sources-index.md per curated couple folder.\n'
            'Requires a fresh .cache/index.sqlite (run `fha index` first).'
        ),
    )
    si.add_argument('person_id', nargs='?', metavar='P-id',
                    help='Person to generate for.')
    si.add_argument('--all-curated', action='store_true',
                    help='Generate per-person and couple-folder files for all curated persons.')
    si.add_argument('--couple-folders', action='store_true',
                    help='Generate sources-index.md in every curated couple folder.')
    si.add_argument('--format', choices=['md', 'html'], default='md', dest='format',
                    help=_FORMAT_HELP)
    si.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    si.set_defaults(func=_cmd_sources_index)

    # ── draft-queue ───────────────────────────────────────────────────────────
    dq = vsubs.add_parser(
        'draft-queue',
        help='Generate per-person draft-queue (uncited-claim backlog) view.',
        description=(
            'Generate {name}_draft-queue_{P-id}.md in the person\'s folder.\n'
            'Requires a fresh .cache/index.sqlite (run `fha index` first).'
        ),
    )
    dq.add_argument('person_id', nargs='?', metavar='P-id',
                    help='Person to generate for.')
    dq.add_argument('--all-curated', action='store_true',
                    help='Generate for every curated person.')
    dq.add_argument('--format', choices=['md', 'html'], default='md', dest='format',
                    help=_FORMAT_HELP)
    dq.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    dq.set_defaults(func=_cmd_draft_queue)

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
                         'and an Apply? confirmation - tier flip, move into the '
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
    tr.add_argument('person_id', metavar='P-id', help='Seed person for traversal.')
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
        help='Output format (default: json). html is not available yet - it '
             'arrives with the site-wide family tree; run `fha site` for a '
             'browsable tree today.',
    )
    tr.add_argument('--out', metavar='FILE', help='Write output to FILE instead of stdout.')
    tr.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    tr.set_defaults(func=_cmd_tree)

    # ── clean ─────────────────────────────────────────────────────────────────
    cl = vsubs.add_parser(
        'clean',
        help='Delete every generated view file (companion .md + generated/views/ HTML); '
             'they regenerate with `fha views refresh`.',
        description=(
            'Delete all GENERATED-headed view files: companion .md files (timeline,\n'
            'sources-index, draft-queue) from the people/ tree, and single-file HTML\n'
            'views under generated/views/. Uses the <!-- GENERATED … --> header as\n'
            'the sole signal, file by file; never touches profiles or manually\n'
            'authored files. --dry-run lists what would be removed without writing.'
        ),
    )
    cl.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    cl.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='List what would be removed without writing.')
    cl.set_defaults(func=_cmd_clean)

    # ── refresh ───────────────────────────────────────────────────────────────
    rf = vsubs.add_parser(
        'refresh',
        help='Regenerate all content views for every curated person and couple folder.',
        description=(
            'Regenerate timeline, draft-queue, and sources-index for every curated\n'
            'person, plus sources-index.md for every curated couple folder.\n'
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
        ('sources-index', 'Generate per-person sources-index view.', _cmd_sources_index, _add_si_args),
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

    rf = subs.add_parser('refresh', help='Regenerate all content views for every curated person and couple folder.')
    rf.add_argument('--format', choices=['md', 'html', 'both'], default='md', dest='format')
    rf.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    rf.set_defaults(func=_cmd_refresh)

    tr = subs.add_parser('tree', help='Traverse relationships and emit an ancestor/descendant/fan tree.')
    tr.add_argument('person_id', metavar='P-id', help='Seed person for traversal.')
    tr.add_argument('--mode', choices=['ancestors', 'descendants', 'fan'], required=True)
    tr.add_argument('--generations', type=int, metavar='N')
    tr.add_argument('--format', choices=['json', 'dot', 'html'], default='json', dest='format')
    tr.add_argument('--out', metavar='FILE')
    tr.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    tr.set_defaults(func=_cmd_tree)


def _add_person_curated_args(p: argparse.ArgumentParser) -> None:
    p.add_argument('person_id', nargs='?', metavar='P-id')
    p.add_argument('--all-curated', action='store_true')
    p.add_argument('--format', choices=['md', 'html'], default='md', dest='format')


def _add_si_args(p: argparse.ArgumentParser) -> None:
    p.add_argument('person_id', nargs='?', metavar='P-id')
    p.add_argument('--all-curated', action='store_true')
    p.add_argument('--couple-folders', action='store_true')
    p.add_argument('--format', choices=['md', 'html'], default='md', dest='format')


if __name__ == '__main__':
    sys.exit(main())
