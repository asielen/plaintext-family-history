#!/usr/bin/env python3
"""
site.py - fha site: the static-HTML family explorer (TOOLING §12).

  fha site [--out PATH] [--standalone | --linked] [--dry-run] [--root PATH]

ARCHITECTURE OVERVIEW
----------------------
`fha site` renders the whole archive as a browsable, fully-relative-link
website that opens straight from `file://` - no server, no CDN, no JS
framework. It is a *snapshot*, not a live view: structured data is read from
`.cache/index.sqlite` (so the site is exactly as fresh as the last
`fha index`), prose (biography, Stories) is read from the curated person
`.md` file, the citation text is read from the source `.md` frontmatter
(the index does not carry it), and the photo strip is read from
`.cache/photos.sqlite` when present. Prose still inside `<!-- AI-DRAFT … -->`
markers is not yet content (AGENTS.md: it stays there "until the human
accepts it" via `fha confirm draft`), so both build modes exclude it - and a
DAMAGED marker (e.g. a missing `-->`) withholds that person's Biography and
Stories entirely, with a warning naming the file: when draft can no longer be
told from accepted prose, publishing nothing is the only safe rendering.

Two build modes, one generator:
  - `--standalone` (default): the safe-to-share snapshot. Living/unknown
    persons - and `restricted` persons (any value, SPEC §21) - get no page and
    render as "Living Person"; restricted, DNA, and `rights.publication_ok:
    false` sources get no page and render as "Restricted - not included in this
    publication"; a single `restricted` claim is withheld even when its source
    publishes; a restricted name (a deadname) resolves internally but redacts to
    the person's unrestricted display name (SPEC §18); image assets become
    web-optimized, EXIF-stripped derivatives copied into `site/media/` so the
    snapshot depends on nothing outside itself.
  - `--linked`: a fast *local* developer preview. Real archive paths (no
    copies), no redaction guarantees. Never hand this folder to anyone.

This file ships the whole Layer 8 publication suite: M8.1 (foundations: query
layer, Jinja2, source page), M8.2 (curated person page), M8.3 (place +
discoveries pages), M8.4 (home page: surname A-Z + discoveries teaser, and the
standalone link/page symmetry enforced by the page-set design below), M8.5
(interactive trees - a vendored, dependency-free renderer fed the neutral tree
JSON through a single adapter seam), and #115 (home page redesign: the home
page's centrepiece is now a marriage-aware ancestor pedigree - the same static
SVG engine the person page already used, scaled to a configurable depth and
re-seeded per build mode - not the interactive descendant explorer, which
moved to a per-person opt-in link; see `_SiteBuilder._build_home_pedigree`).

Note what the page-set design is and is not. It guarantees that this site
never LINKS to a page it did not build; it does not check whether a page
SHOULD have been built, and there is no separate audit pass that does. So
every input to the page-set decision has to be right on its own, and the ones
read from disk (`_load_restriction_markers`) fail closed: a person or source
record this build could not read is treated as restricted and withheld, with a
warning naming the file. A missing privacy marker is indistinguishable from no
privacy marker, and only one of those two readings is safe to publish.

WHY A LIBRARY FUNCTION (`run_site`): mirrors packet/report - a testable
`run_site(archive_root, out_dir, ...) -> dict` core, with a thin CLI handler
that turns the result into exit codes and stdout. Tests drive `run_site`
against a synthetic index without touching the real archive.

REDACTION IS COMPUTED ONCE, UP FRONT. `_SiteBuilder` decides the set of
person/source pages that will exist *before* rendering any page, so every
token-swap and every cross-link consults the same authoritative set: a page
is linked iff it is in that set. A page that isn't generated is never linked
to (the M8.4 symmetry rule, enforced here from the start).

DEPENDENCIES. Jinja2 (templates) is required. Pillow (PIL) is *optional*:
standalone image derivatives use it when present; when absent, standalone
simply omits images with a plain note rather than copying originals (which
would leak the EXIF the snapshot is meant to strip). `--linked` never needs
PIL.

CODE MAP
--------
  Prose / HTML
    _escape                    - html.escape shorthand
    (strip_unaccepted_drafts   - drop `<!-- AI-DRAFT … -->` prose + AI markers,
                                 fail-closed on damaged markers - lives in _lib)
    _safe_link_href            - markdown-link scheme allowlist (stored-XSS guard)
    _prose_to_html             - minimal stdlib markdown→HTML (no md library)
    _inline_html               - inline pass: links, [ID] tokens, **bold**;
                                 also where `_scrub_internal_encoding` runs
                                 (per literal span, after links are matched)
    _rendered_boundary_char    - the real first visible character of a
                                 construct's OWN rendered HTML, not its
                                 pre-render label - `_inline_html`'s spacing
                                 decisions read this, not a guess
    _scrub_internal_encoding   - drop a bare (C-xxxx) parenthetical / translate
                                 a [..YYYY] "before" bracket (or a two-sided
                                 [..YYYY]/YYYY interval) / redact a bare
                                 [PSCLH]-xxxxxxxxxx citation id (`_BARE_ID_RE`,
                                 a defense-in-depth backstop for a citation
                                 that leaked past `_INLINE_RE` unmatched) in
                                 free-text claim values, wherever one reaches
                                 a reader-facing page (prose, timeline,
                                 source/place claims tables, person summary
                                 vitals)
    _extract_section           - pull one `## Heading` section body from a record
    _question_block_body       - a '## Q:' block's body, heading dropped, cut
                                 before the next heading of any kind (#117)

  Dates
    _decade_header             - EDTF date → "1880s" decade label (timeline grouping)

  Places
    _place_leading_component   - a place label's text before its first top-level
                                 comma (parentheticals stripped first) - "Millbrook"
                                 out of "Millbrook, Dutchess County, New York"
    _place_leading_parts       - the leading component split on "and" into its
                                 coordinate names ("Trinidad and Tobago" ->
                                 ["Trinidad", "Tobago"]), or the component itself
                                 when it names one place, not a compound
    _match_place_words         - whole-word, loose-punctuation search for one
                                 label's words in a sentence; span or None
    _match_coordinated_place_parts - like `_match_place_words`, but for >= 2
                                 coordinate parts: requires them to appear in
                                 order as ONE "part1 ... and ... part2" mention
                                 (parenthetical elaboration allowed around the
                                 "and"), not just anywhere independently (#127
                                 reopened, finding 2 follow-up)
    _place_mention_span        - where a claim's own sentence already names its
                                 place (so the timeline prints it once, linked)
    _place_trailing_remainder  - the label's droppable qualifier ("Dutchess
                                 County, New York") to print as a sentence
                                 continuation when the place has no linkable
                                 page - the fuller name would otherwise vanish
                                 with no link to recover it (#127 reopened,
                                 finding 1 follow-up)

  Image derivatives
    _PIL_AVAILABLE             - is Pillow importable?
    _make_derivative           - resized, EXIF-stripped JPEG/PNG copy (standalone)

  Static charts (person page + home page, #115)
    _render_fan_svg            - radial ancestor fan, self-contained SVG
    _ancestor_branch           - Ahnentafel slot → paternal(1)/maternal(2) line
    _render_pedigree_svg       - horizontal family chart: siblings/children -
                                 subject/spouse(s) - N ancestor generations,
                                 self-contained SVG (person page: 2 generations,
                                 no siblings; home page, #115: configurable
                                 depth, siblings, branch coloring, axis label)

  Photo-catalog keys
    _live_alias                - the real path under a reconcile 'MISSING:' key
    _is_missing_key            - is this catalog key a photo that is not on disk?
    _under_ignored_path        - does a photos-root path fall under photos_ignore:?
    _under_ignored_dir         - the same, for an absolute folder (sifts the
                                 unreadable list before it can spoil a count)

  Paths / hrefs
    _rel_href                  - relative href from a page dir to a target file
    _page_filename             - id → 'p-xxx.html' / 's-xxx.html'
    _json_for_script           - JSON serialized safe for inline <script> embedding

  Interactive tree (M8.5, now per-person - #115) + shared chart redaction
    _apex_ancestor             - #115: repurposed from "deepest ancestor of
                                 root_person" (the old home-tree seed) to the
                                 home pedigree's redaction-safe hub walk -
                                 closest non-living/non-redacted ancestor
    _build_tree_data           - BFS relationships → neutral tree JSON + url + redaction
    _tree_node, _person_vitals - one redacted node; its birth/death labels
                                 (scoped to the person's OWN vitals, #126)
    _chart_entry               - one redacted {name,url,dates} node; shared by the
                                 Ahnentafel walk and the family-wings walk below
    _build_ahnentafel          - parent-edge walk → Ahnentafel map (fan + pedigree),
                                 already generation-agnostic (unchanged by #115)
    _build_family_wings        - spouse/child edges → pedigree's family-chart
                                 columns; `include_siblings` (#115) adds a third
                                 list for the home pedigree hub only
    _hub_siblings              - #115: the home pedigree hub's own siblings -
                                 everyone else sharing one of its recorded parents
    _make_tree_ctx             - build a tree, write data/tree_*.json, return template ctx
    _copy_vendor               - copy the vendored renderer/adapter into the site

  Builder
    _SiteBuilder               - holds conn, mode, maps, page sets, jinja env
      .prepare                 - load persons/sources, decide which pages exist
      ._load_open_questions    - index open '## Q:' blocks by referenced person
                                 id (#117; linked-or-workbench, see build_person_page)
      ._claim_is_own_vital     - is this vital claim a record OF this person,
                                 or of a relative it also names? (roles:, #126)
      ._person_summary         - the Born/Died/Married infobox, own vitals only
      ._person_open_questions  - this person's open questions, rendered
      .render_token            - one [ID] token → HTML (link / redaction / mark)
      .build_source_page       - M8.1 source page
      .build_person_page       - M8.2 person page; also builds `descendants_tree`
                                 (#115: the interactive explorer, demoted here
                                 as a per-person opt-in link)
      ._home_pedigree_depth    - #115: site.home_pedigree_generations, clamped
      ._build_home_pedigree    - #115: resolve the redaction-safe hub, build
                                 and render the home page's ancestor pedigree
      .build_index_page        - home page: surname index, discoveries teaser,
                                 sources/places nav, and the home pedigree (#115)
      .run                     - orchestrate: prepare, build all pages, write

  Core / CLI
    run_site                   - library entry point
    _unowned_output_reason     - refuse a non-empty --out fha site didn't create
    _cmd_site, register, _standalone_main
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import html
import json
import math
import os
import re
import shutil
import sqlite3
import sys
from collections import deque
from pathlib import Path
from urllib.parse import quote as _urlquote

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    apply_private_fence,
    ASSET_ROOT_ALIASES,
    claim_is_own_vital,
    configure_utf8_stdout,
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    extract_bare_ids,
    FhaConfigError,
    fmt_id_display,
    humanize_edtf as _humanize_edtf,
    id_type_of,
    is_genetic_parent_subtype,
    is_valid_edtf,
    is_working_copy,
    load_fha_yaml,
    normalize_id,
    open_index_db,
    parse_filename,
    parse_questions,
    person_section_is_unfilled,
    photoindex_status,
    photos_ignore_matcher,
    photos_ignore_patterns,
    pip_command,
    PROVISIONAL_VITAL_FIELDS,
    read_record,
    read_text_or_report,
    resolve_path,
    resolve_root_arg,
    Result,
    split_log_entries,
    strip_link_wrapper,
    strip_unaccepted_drafts,
    unreadable_dir_recorder,
    walk_files,)

configure_utf8_stdout()

try:  # Jinja2 is a required dependency for this tool (TOOLING §12); guard the
    # import only so the CLI can print a plain install hint instead of a traceback.
    import jinja2
except ModuleNotFoundError:  # pragma: no cover - exercised via the CLI guard
    jinja2 = None  # type: ignore[assignment]

try:  # Pillow is OPTIONAL - standalone image derivatives use it when present.
    from PIL import Image
    _PIL_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - environment-dependent
    Image = None  # type: ignore[assignment]
    _PIL_AVAILABLE = False


_REQUIRED_TABLES = (
    'persons', 'sources', 'claims', 'claim_persons', 'source_files',
    'source_people', 'relationships', 'places', 'person_files',
    'place_names', 'place_history',
)

_IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.heic', '.bmp', '.gif'}

# `fha photoindex reconcile` keeps a vanished photo's catalog row under the
# synthetic key 'MISSING:' + its last known path, so the caption, keywords and
# person tags it carried outlive the file. photoindex.py owns the rule; it is
# restated here because tools never import tools (TOOLING §15). The site is a
# page of pictures: anything that ends in an <img> must ask _is_missing_key
# first, while anything that asks WHERE a photo lived (or WHO is in it) reads
# through _live_alias instead.
_MISSING_PREFIX = 'MISSING:'


def _live_alias(path: str) -> str:
    """The alias a cached photo key names, with any 'MISSING:' prefix off."""
    return path[len(_MISSING_PREFIX):] if path.startswith(_MISSING_PREFIX) else path


def _is_missing_key(path: str) -> bool:
    """True when a cached photo path is reconcile's synthetic missing-file key.

    Such a row can never produce an image: there is no file to copy, resize, or
    link to. Filtering on it early also keeps a vanished variant from being
    chosen as its group's representative and taking a still-present sibling
    down with it.
    """
    return path.startswith(_MISSING_PREFIX)


def _under_ignored_path(rel: str, is_ignored) -> bool:
    """True when `rel` (posix, relative to the photos root) is itself excluded
    by `photos_ignore:`, or sits inside a folder that is.

    The scan walks with `os.walk` and prunes a matching directory before
    descending, so a pattern like 'Flickr Export' never has to match the files
    beneath it. A `rglob` finds those files directly, so each ancestor folder
    is tested here instead - otherwise the site would publish out of exactly
    the subtree the setting exists to keep out of the library.
    """
    parts = rel.split('/')
    return any(is_ignored('/'.join(parts[:i])) for i in range(1, len(parts) + 1))


def _under_ignored_dir(directory: Path, photos_root: Path, is_ignored) -> bool:
    """The same question as `_under_ignored_path`, asked of an absolute folder.

    Used to sift the folders a photos-root walk could not open before they are
    allowed to spoil a uniqueness count. A folder the setting excludes holds
    nothing this build would have used, so it can hide nothing from the count
    either, and refusing over it would be a refusal the guard has not earned.

    A path that is not under the photos root at all (or is the root itself) is
    reported as NOT ignored: the honest answer for a folder the patterns say
    nothing about is that it still counts.
    """
    try:
        rel = Path(directory).resolve().relative_to(Path(photos_root).resolve()).as_posix()
    except (ValueError, OSError):
        return False
    if rel in ('', '.'):
        return False
    return _under_ignored_path(rel, is_ignored)


# Why the photo catalog cannot answer, in the words `photoindex_status` returns,
# said the way the archive's owner would say it. The living-person photo gate
# reads that catalog, so when it is not usable the gate cannot fire and the build
# has to say so (see `_SiteBuilder._warn_living_photo_check_unavailable`).
_PHOTO_CATALOG_TROUBLE = {
    'absent': 'the photo catalog has not been built yet',
    'stale': 'the photo catalog is out of date',
    'unreadable': 'the photo catalog could not be read',
    'old-schema': 'the photo catalog was built by an older version of the tools',
}

# The largest edge (px) a standalone derivative is resized to (TOOLING §12).
_DERIVATIVE_MAX_PX = 1200
# Profile photos are shown small (a person-page plate, a tree square), so their
# derivative is kept light - crisp at portrait size, tiny for the tree thumbnail.
_PROFILE_MAX_PX = 512

# Ancestor generations drawn in the static fan chart (rings beyond the subject).
# 3 = up to great-grandparents. Every ring then keeps its labels on a roomy
# *curved* arc (the 4th ring would need cramped radial spokes that clip long
# names); the fan auto-shrinks to the actual depth present, so a shallow tree
# still renders small. One generation deeper than the person-page pedigree.
_FAN_GENERATIONS = 3

# Home pedigree ancestor depth (#115), overridable via
# `site.home_pedigree_generations`. DEFAULT is the decided answer to the
# issue's open question #1 - deep enough (great-great-great-grandparents) to
# read as a real pedigree chart; a deeper ancestor is one click away on their
# own re-centered page (#115's "click-through, not infinite canvas" design),
# not a config knob most owners need to touch. MAX guards the config knob
# itself: each extra generation DOUBLES the ancestor-slot count
# (2**(N+1) slots), so an unclamped typo could try to lay out an
# astronomically large chart - see `_SiteBuilder._home_pedigree_depth`.
_HOME_PEDIGREE_GENERATIONS_DEFAULT = 5
_HOME_PEDIGREE_GENERATIONS_MAX = 8

# Server-side hop SAFETY NET for each curated person's descendant-explorer
# BFS (#152 review fix, P2 - performance; raised + re-scoped by a second
# #152 follow-up review after the first cut of this fix - see below).
# `build_person_page` used to call `_make_tree_ctx` with `max_hops=None`
# (fully unbounded) for every curated person's opt-in descendant tree, even
# though the disclosure is collapsed by default and the client only ever
# PAINTS `initial_depth` generations at once. Because descendant subtrees
# overlap heavily along one lineage (a person's descendants include their
# children's descendants, and so on), an unbounded walk per page made a
# lineage of N curated people redo roughly O(N^2) worth of `relationships`
# queries across one site build - each ancestor's page re-issuing almost the
# same SELECTs the page below it already ran in full.
#
# The first cut of this fix used this constant as a hard TRUNCATION bound,
# which silently dropped any descendant more than 12 hops from the seed from
# BOTH the embedded tree and the reusable `data/tree_*.json` artifact -
# contradicting `tools/README.md`'s own promise that "only the initial paint
# is bounded while the data stays complete and the reader expands forward."
# The real O(N^2) cost was the repeated `relationships` SELECT per shared
# descendant, not the size of any one page's JSON, so `_tree_edges_cache`
# (memoized in `__init__`, read via `_tree_relationship_rows`) now caches
# those rows for the whole build: a person reached by many overlapping
# walks is queried once, however many pages' trees pass through them. That
# turns the real cost from O(N^2) queries into O(N) queries (plus O(N^2)
# cheap in-memory dict/edge construction, which is orders of magnitude
# cheaper than the SQL round trips it replaces, and trivial at any archive
# size a human genealogy actually reaches).
#
# With the query cost solved, this constant goes back to being what a hop
# bound like this should be: a generous safety net against a pathological
# or corrupt dataset, not a visible content limit - raised far past any
# depth a real family tree reaches, with `_build_tree_data` warning loudly
# (never silently) if a walk ever actually hits it. See `build_person_page`'s
# `descendants_tree` context and `_build_tree_data`'s `truncated` handling.
_DESCENDANT_TREE_MAX_HOPS = 500

# Redaction display strings (M8 UX bar: redacted content is named, never a blank).
_LIVING_LABEL = 'Living Person'
_RESTRICTED_LABEL = 'Restricted - not included in this publication'
# Registry key for the single shared "restricted source" footnote: every withheld
# source citation on a page collapses to one entry, so the count/identity of the
# restricted sources is never revealed and the label never repeats inline.
_RESTRICTED_FN = '\x00restricted'

# Ownership stamp written into the output dir after every successful build.
# `_reset_output` clears generic-named subtrees (sources/, media/, ...), so a
# rebuild must first prove the target dir is ours - see _unowned_output_reason.
_SITE_MARKER_NAME = '.fha-site'

# Summary-block label per vital claim type (M8.2 "summary block (accepted vitals)").
_VITAL_LABELS = {
    'birth': 'Born', 'death': 'Died', 'marriage': 'Married',
    'baptism': 'Baptized', 'burial': 'Buried',
}
_VITAL_ORDER = ['birth', 'baptism', 'marriage', 'death', 'burial']

# Friends & Family grouping, in display order (TOOLING §12). These mirror the
# relationship edge types the index actually derives (M1.3): parent/child/spouse
# from vital+relationship claims, and friend/associate/neighbor from social
# subtypes. No 'sibling' edge is derived, so it is intentionally absent.
_FAMILY_GROUPS = [
    ('parent', 'Parents'),
    ('spouse', 'Spouses'),
    ('child', 'Children'),
    ('friend', 'Friends'),
    ('associate', 'Associates'),
    ('neighbor', 'Neighbors'),
]


# ── Undecodable records (#68 follow-through; see `_lib.read_record`'s docstring) ──
# Left at its default, `read_record` raises `UnicodeDecodeError` on a file
# saved in another codepage (cp1252, a Windows editor's default). Every site
# below opts in to the reporting shape instead, in one of two ways depending
# on what it already does with the failure:

def _raise_friendly_decode_error(path: Path) -> None:
    """`on_decode_error` for a site that already catches its read in
    `except Exception as e:` and shows `str(e)` to the human inside a
    WARNING naming the file. Left unset, that `e` would be the raw
    `UnicodeDecodeError` - accurate, but stated in byte offsets and codec
    names that mean nothing to a non-technical reader. Raising a friendlier
    exception here lands in that same `except` block (Python allows a new
    exception to be raised from inside a callback an `except` calls), so it
    swaps only what `str(e)` says - no call site's control flow changes.
    Wording echoes `fha lint`'s W128 (and `fha index`'s undecodable-files
    note) - same cause, same fix, said in one line rather than W128's full
    paragraph, because this one arrives inside a build report."""
    raise ValueError(
        "this file isn't saved as UTF-8 text - a Windows editor's default "
        "encoding, often cp1252, is the usual cause; open it and save it "
        "again choosing UTF-8"
    )


def _ignore_decode_error(path: Path) -> None:
    """`on_decode_error` for a site that branches on `rec['undecodable']`
    itself rather than showing `str(e)` anywhere. Supplying ANY callable
    (even one that does nothing) is what turns a bad decode from a raise
    into the empty record `read_record` returns for a caller who asked to
    be told - see its docstring. A bare `except Exception` could not have
    told a decode failure apart from a real parse error; this makes the two
    paths explicit even at a site where they happen to share one fallback."""
    return None


def _today() -> str:
    return datetime.date.today().isoformat()


# ── The `restricted` marker (SPEC §19, §21) ────────────────────────────────────
# A standalone snapshot is public output, so anything `restricted` - a source, a
# claim, a person, or a name - is excluded wherever it appears, with no opt-in.
# The index carries no claim/person/name-level `restricted`, so those are read
# from the record files in `_load_restriction_markers`. One truthiness test:

def _is_restricted_value(value) -> bool:
    """True when a `restricted:` value withholds a record from public output.

    The marker is open (SPEC §19): the plain boolean `true` or any free-text
    type all mean restricted; only absent/false is not. (`read_record` coerces
    booleans to `'true'`/`'false'`.) Public output has no opt-in - even
    `restricted: by-request` is honored - so a single truthiness test suffices."""
    return value not in (None, False, '', 'false')


def _restricted_type(value) -> str | None:
    """Normalize a raw `restricted:` value to its type, or None when unrestricted.

    Duplicated from `packet.py`'s own `_restricted_type` (tools never import
    tools, TOOLING §15) - the two must agree exactly on the contract. Every
    OTHER restriction check in this module only needs the open yes/no
    `_is_restricted_value` test above, because every other check is gated on
    `not self.linked` (a plain `--linked` preview has no privacy contract to
    keep, `UnreadableRecordPrivacyTests.test_linked_mode_is_unchanged`). The
    one exception is the `by-request` no-override tier (SPEC §19: "honored by
    every export path with no opt-in") consulted by `_person_is_by_request`,
    which runs precisely when `self.linked` and so needs to tell `by-request`
    apart from a plain or other-typed restriction, not just detect one."""
    if value in (None, False, '', 'false'):
        return None
    if value in (True, 'true'):
        return 'plain'
    return str(value).strip().lower() or 'plain'


# ── Prose / HTML ────────────────────────────────────────────────────────────

def _escape(text: str) -> str:
    """html.escape, never quoting - we only emit text into element bodies here."""
    return html.escape(text, quote=False)


# The AI-DRAFT prose exclusion (strip_unaccepted_drafts) lives in _lib - one
# implementation shared with fha wikitree, fail-closed on damaged markers.
# Consumed in _person_prose below.


# Schemes a markdown link in prose may carry. Anything else scheme-bearing
# (javascript:, data:, vbscript:, file:, ...) renders as plain text.
_ALLOWED_LINK_SCHEMES = ('http', 'https', 'mailto')


def _safe_link_href(raw_url: str) -> str | None:
    """Escaped href for a markdown link, or None when its scheme is not allowed.

    Why: `[x](javascript:alert%281%29)` or a `data:` URI in a biography would
    otherwise emit a live href - stored XSS in a site that gets handed to
    relatives. Per the URL grammar (RFC 3986) a URL carries a scheme exactly
    when its first `:` comes before the first `/`, `?`, or `#`; such URLs may
    link only with an http/https/mailto scheme (case-insensitive). Scheme-less
    relative URLs (`sub/page.html`, `#top`, `./a:b`) keep linking.
    """
    head = re.split(r'[/?#]', raw_url, maxsplit=1)[0]
    if ':' in head:
        scheme = head.split(':', 1)[0].lower()
        if scheme not in _ALLOWED_LINK_SCHEMES:
            return None
    return html.escape(raw_url, quote=True)


# Inline constructs, tried left to right. The `[[ ]]` wikilink is matched
# before the markdown link `[text](url)` (see the paragraph below - #167
# finding 3), which in turn is matched before the legacy single-bracket
# `[ID]` token so a token never half-matches a link; bold is last. Anything
# not matched is literal text and gets escaped.
#
# Wikilink-before-markdown-link is NOT the original order - it was flipped
# for #167 finding 3 (regression from the `ltext` bracket-nesting widening
# below): once `ltext` tolerates one balanced `[...]` unit inside a
# markdown-link label, a `[[S-id|display]]` wikilink immediately followed by
# `(text)` - e.g. `[[S-1111111111|source]](note)` - looks, from `ltext`'s
# perspective, exactly like a label consisting of one bracketed unit
# (`[S-1111111111|source]`) followed by a link URL (`note`): the wikilink's
# own OUTER `[` becomes `ltext`'s opening bracket, the nested unit swallows
# `[S-1111111111|source]` as a single balanced-bracket repetition, and the
# wikilink's own second closing `]` becomes `ltext`'s closing bracket - a
# complete, well-formed (but WRONG) markdown-link match. Since alternation
# tries alternatives in listed order at each starting position, having the
# markdown-link alternative listed first let it win here even though the
# text is unambiguously a wikilink, silently losing the citation (Codex
# review of #167).
#
# A negative lookahead on `ltext`'s opening bracket (reject a `[` directly
# followed by another `[`) was considered first and rejected: it also
# blocks the legitimate widened case `[see [..1900] record](url)`, whose
# very first content IS a single bracketed unit starting with `[` - that
# shape and the wikilink-stealing shape both start with two consecutive
# `[` characters, so a lookahead that only peeks one character ahead cannot
# tell them apart, and correctly telling them apart requires re-deriving
# the wikilink grammar (target/`#anchor`/`|display`, ending `]]`) inside the
# lookahead - fragile, easy to drift from the real `wtarget`/`wdisp` pattern
# next time either changes. Reordering instead relies on the wikilink
# alternative's OWN stricter shape to self-select correctly: it only
# matches when an immediate `]]` genuinely closes the construct, which is
# true for `[[S-1111111111|source]](note)` (wins the wikilink alternative,
# leaving `(note)` as literal text - restoring the pre-widening behaviour
# Codex's report describes) but false for `[see [..1900] record](url)`
# (only one `]` follows the nested date unit, not two, so the wikilink
# alternative fails outright and falls through to the markdown-link
# alternative exactly as before). No other alternative overlaps with the
# wikilink's own `[[` prefix (the legacy token's `[` is immediately
# followed by an id character, never another `[`), so this reordering
# changes nothing else about what matches where.
# `lurl` allows one level of BALANCED parens inside the URL (P2, PR #158
# follow-up) - a plain `[^)\s]+` stops at a URL's own first `)`, which is
# wrong the moment the URL legitimately contains one, e.g. a claim-id
# parenthetical pasted into a query string
# (`https://example.test/search?ref=(C-4kx9m2p7qr)`) or a Wikipedia-style
# disambiguation path. Without balancing, the group stops at the inner `)`,
# producing a TRUNCATED href plus a stray `)` leaking as literal text right
# after the closing `</a>` - the link still "matches" but points at the
# wrong, cut-off target. `(?:[^()\s]|\([^()\s]*\))+` matches runs of
# non-paren/non-space characters OR one complete `(...)` unit at a time, so
# a `(...)` pair inside the URL is consumed whole and only the link's OWN
# closing paren (matched separately, right after this group) ends the URL.
# `wdisp` (a wikilink's `|display` label) needs the SAME one-level-of-
# balancing trick, for square brackets instead of parens (#167 finding 2,
# privacy-adjacent): a plain `[^\[\]]*` cannot match ANY `[` or `]`, so a
# label that legitimately contains SPEC §11 before-date notation, e.g.
# `[[S-1111111111|record filed [..1905]]]`, made the WHOLE wikilink
# alternative fail to match at that position - `_INLINE_RE` then had no
# alternative left that matched, so the entire construct fell through as
# ordinary literal text. Literal text only gets HTML-escaped and scrubbed
# of claim-id-parens/date-brackets (`_scrub_internal_encoding`); it is NOT
# scanned for bare `S-xxxx`/etc. id tokens (that is `render_token`'s job,
# reached only through a MATCHED `[[...]]`/`[ID]` construct) - so the raw
# `S-1111111111` id leaked onto the page verbatim, unlinked and unscrubbed.
# `(?:[^\[\]]|\[[^\[\]]*\])*` mirrors `lurl`'s technique: a non-bracket
# char, OR one complete `[...]` unit with no brackets of its own inside it,
# repeated. A lone `]` (including the first `]` of the wikilink's own
# terminating `]]`) matches neither alternative, so the group always stops
# there - it cannot swallow the construct's own closing `]]`.
#
# `ltext` (a markdown link's `[text](url)` label) gets the identical
# treatment for the identical reason (adversarial review of PR #158's own
# `wdisp` fix, #167 finding 2 continued): it was left as the original
# `[^\]]+` - a single unmatched `]` inside the label, e.g.
# `[see S-1111111111 [..1905]](url)`, broke the WHOLE markdown-link
# alternative the same way `wdisp`'s did, and the raw id leaked the same
# way, still unlinked. `+` (not `*`) keeps the pre-existing "label is
# non-empty" requirement.
#
# Neither widening is a complete fix by itself - no FIXED-DEPTH regex can
# balance brackets nested arbitrarily deep (two-plus levels, e.g.
# `[[S-id|a[b[c]d]e]]`), or genuinely unbalanced ones (a stray `[` or `]`
# with no partner at all) - the construct still falls through as ordinary
# literal text for those shapes, exactly as before either widening existed.
# `_BARE_ID_RE` (see `_scrub_internal_encoding`) is the DEFENSE-IN-DEPTH
# backstop for that residual case: literal text - unlike a link/wikilink
# TARGET, which is never scrubbed - is always scanned for a bare id shape
# before it reaches the page, so even a construct `_INLINE_RE` never
# recognizes as a link can no longer leak its raw citation id verbatim.
_INLINE_RE = re.compile(
    r'\[\[(?P<wtarget>[^\[\]|#]+)(?:#[^\[\]|]*)?'
    r'(?:\|(?P<wdisp>(?:[^\[\]]|\[[^\[\]]*\])*))?\]\]'            # [[target|disp]]
    r'|\[(?P<ltext>(?:[^\[\]]|\[[^\[\]]*\])+)\]\((?P<lurl>(?:[^()\s]|\([^()\s]*\))+)\)'  # [text](url)
    r'|\[(?P<token>[PSCLH]-[0-9a-hjkmnp-tv-z]{10})\]'            # legacy [ID] token
    r'|\*\*(?P<bold>.+?)\*\*',                                    # **bold**
    re.I,
)


def _inline_html(text: str, render_token) -> str:
    """Render one block of inline prose to HTML.

    Handles markdown links (scheme-checked by `_safe_link_href`; a disallowed
    scheme renders the label as plain text), archive citation tokens
    (`[[ID|display]]` / `[[name]]` / legacy `[ID]`, delegated to `render_token`,
    which already returns safe HTML), and `**bold**`. Every run of literal text
    between constructs is HTML-escaped, so a stray `<` in a biography can never
    inject markup. `render_token` is the only source of un-escaped HTML and it
    is fully under our control (it emits anchors and spans we build).

    `render_token(target, display=None)` accepts an ID *or* a human name/stem
    (resolved through the alias map) plus the optional in-token display text.

    Internal-only encoding (`_scrub_internal_encoding` - #140, #144 finding
    3) is scrubbed HERE, per literal-text run and per construct's own visible
    label, AFTER `_INLINE_RE` has already matched links/tokens/bold against
    the UNSCRUBBED text - never as a pre-pass over the whole raw block.
    Scrubbing first (the old order, in `_prose_to_html`) could rewrite a
    `[..1905]`-shaped or `(C-xxxxxxxxxx)`-shaped substring sitting INSIDE a
    markdown link's URL before `_INLINE_RE` ever got a chance to recognize
    `[text](url)` as a link at all - corrupting the target and breaking the
    link syntax outright (issue reproduces with e.g.
    `[record](https://example.test/search/[..1905])`). A link/wikilink
    TARGET (`lurl`/`wtarget`) is therefore never scrubbed - it is a URL or a
    record id, not prose a reader reads as English - only literal text and a
    visible label (`ltext`, `wdisp`, `bold`) are. `wdisp` (a wikilink's
    `|display` text, e.g. `[[S-id|the record (C-xxxx)]]`) was originally
    missed here (P2, PR #158 follow-up) - it reaches `render_token`, whose
    source/person/place renderers only HTML-escape it, so an internal claim
    id left unscrubbed in a wikilink's label leaked straight onto the
    reader-facing page even though the equivalent markdown-link label
    (`ltext`) was already covered.

    Each literal-text run is scrubbed independently (#167 finding 1): when a
    claim-id parenthetical (or, since the adversarial-review follow-up, a
    bare citation-id token - see `_BARE_ID_RE`) sits at the very END of a
    run - e.g. "The record (C-4kx9m2p7qr)" immediately followed by a matched
    `**bold**` or `[link](url)` with no space of its own -
    `_strip_claim_id_paren` (see its docstring) has no way to see what comes
    next in the FULL original text; the substring it is handed simply ends
    at the match. Left alone, it drops the separating space it would
    otherwise reinsert, welding the two surrounding words together
    ("record**confirms**" -> no space before "confirms"). A boundary
    character - "what does a reader see right after this run?" - is passed
    for every run EXCEPT the last (nothing follows the block's final
    literal-text run).

    That boundary character is the ACTUAL next rendered character, read back
    from `rendered` itself AFTER it's been produced - not guessed from any
    pre-render label (adversarial review, round 3: even a SCRUBBED label can
    still disagree with what actually renders). `render_token` does not
    always honor the display text it's handed: a wikilink naming a redacted
    living person, or a withheld/restricted source, ignores `in_display`
    entirely and substitutes fixed replacement markup instead - "Living
    Person", "Restricted - not included in this publication", or a bare
    footnote digit - regardless of what the label said or how it scrubbed.
    `"The record(C-xxx)[[P-id|S-2222222222]]"`, where `S-2222222222` is a
    bare id that itself gets scrubbed to `[record]` and `P-id` resolves to a
    redacted living person, used to compute its boundary from the scrubbed
    LABEL's first character (`[`, not a word character) and correctly-but-
    coincidentally skip a space - the same shape with an ordinary label
    (`[[P-id|Margaret]]`) computed boundary from `M` (a word character) and
    inserted one - even though BOTH actually render as "Living Person" (a
    word character): one of the two was always going to disagree with the
    real output, because neither ever looked at it. `_rendered_boundary_char`
    strips markup off whatever `rendered` turned out to be for every one of
    `_INLINE_RE`'s four branches and reads the first VISIBLE character from
    THAT, so it can never disagree with the page a reader actually sees,
    regardless of whether `render_token` honored, ignored, or overrode the
    label it was given. The one case this can't help is a construct whose
    own render comes back empty (a dangling source citation drops out to
    `''`) - `_INLINE_BOUNDARY_CHAR`, the same conservative "assume a word
    character follows" sentinel as before, is the fallback there.
    """
    out: list[str] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        # Compute this match's own rendered output first - the boundary
        # character its literal-text predecessor should see is read back
        # from THAT (see this function's docstring for why: the rendered
        # HTML is the only thing that can't disagree with itself).
        if m.group('wtarget') is not None:
            scrubbed_wdisp = _scrub_internal_encoding(m.group('wdisp'))
            rendered = render_token(m.group('wtarget').strip(), scrubbed_wdisp)
        elif m.group('token'):
            rendered = render_token(m.group('token'))
        elif m.group('ltext') is not None:
            scrubbed_ltext = _scrub_internal_encoding(m.group('ltext'))
            href = _safe_link_href(m.group('lurl'))
            label = _escape(scrubbed_ltext)
            rendered = f'<a href="{href}">{label}</a>' if href is not None else label
        else:  # bold
            scrubbed_bold = _scrub_internal_encoding(m.group('bold'))
            rendered = f'<strong>{_escape(scrubbed_bold)}</strong>'
        boundary = _rendered_boundary_char(rendered)
        out.append(_escape(_scrub_internal_encoding(text[pos:m.start()], boundary)))
        pos = m.end()
        out.append(rendered)
    out.append(_escape(_scrub_internal_encoding(text[pos:])))
    return ''.join(out)


_HTML_TAG_RE = re.compile(r'<[^>]*>')


def _rendered_boundary_char(rendered: str) -> str:
    """The first VISIBLE (non-markup) character of `rendered`, for
    `_inline_html`'s boundary-spacing decision (adversarial review, round 3
    - see `_inline_html`'s docstring for the bug this replaced: a boundary
    character guessed from a label instead of read back from the render
    itself). Strips HTML tags rather than parsing them - every `rendered`
    value here is markup this module built a line above, never third-party
    HTML, so a plain tag-strip is exact, not an approximation. Falls back to
    `_INLINE_BOUNDARY_CHAR` when nothing visible remains (a dangling source
    citation renders as `''`)."""
    visible = _HTML_TAG_RE.sub('', rendered)
    return visible[0] if visible else _INLINE_BOUNDARY_CHAR


_HEADING_RE = re.compile(r'^(#{1,6})\s+(.*)$')
_LIST_RE = re.compile(r'^\s*[-*]\s+(.*)$')
# A photo embed on its own line: `![[S-id|Caption]]` (Obsidian embed syntax).
# Renders as a figure; the id resolves to a photo through the index. Caption optional.
_EMBED_RE = re.compile(r'^!\[\[\s*([^\]|]+?)\s*(?:\|\s*([^\]]*?)\s*)?\]\]\s*$')

# A bare `(C-xxxxxxxxxx)` claim-id parenthetical (#140) - an internal cross-
# reference, not the sanctioned `[[C-xxxx]]` citation form, and meaningless
# to a reader. Parentheses never collide with the wikilink (`[[ ]]`) or
# legacy-token (`[ID]`) forms `_INLINE_RE` resolves into links, so this is
# safe to drop unconditionally rather than routed through render_token. Same
# id-char class `_INLINE_RE`'s legacy token uses (Crockford Base32, SPEC §10).
_CLAIM_ID_PAREN_RE = re.compile(r'\s?\(C-[0-9a-hjkmnp-tv-z]{10}\)', re.I)

# A bare `[PSCLH]-xxxxxxxxxx`-shaped citation id sitting in literal prose
# text, NOT wrapped in a construct `_INLINE_RE` actually matched (#167
# finding 2 continued, adversarial review of PR #158's fix - privacy-
# critical). `_INLINE_RE`'s `wdisp`/`ltext` label groups now tolerate one
# level of bracket nesting (see `_INLINE_RE`'s own comment), but no FIXED-
# DEPTH regex can balance brackets nested arbitrarily deep, or genuinely
# unbalanced ones - `[[S-1111111111|text [oops]]` (missing a `]`),
# `[[S-1111111111|text ][ stuff]]`, `[[S-1111111111|text [a[b]c]]]` (nested
# two deep), and the markdown-link equivalent
# `[see S-1111111111 [..1905]](url)` all still make the WHOLE construct fail
# to match, falling through as ordinary literal text. Literal text is
# HTML-escaped and scrubbed of claim-id-parens/date-brackets, but - before
# this fix - never inspected for a bare id shape, so the raw id leaked onto
# the reader-facing page verbatim: exactly the class of leak the `wdisp`
# widening (this same file, same review round) was meant to close, just not
# fully. This is a DEFENSE-IN-DEPTH backstop, not a replacement for
# `_INLINE_RE` matching real constructs correctly: `_scrub_internal_encoding`
# is never called on a link/wikilink/token's own TARGET (`lurl`/`wtarget`/
# `token` - see `_inline_html`, which never routes those through it), only
# on literal text and a matched construct's own visible LABEL, so this can
# never re-touch or corrupt an id `_INLINE_RE` already correctly turned into
# a real citation link. Same id-char class as `_INLINE_RE`'s legacy `[ID]`
# token (Crockford Base32, SPEC §10); the lookarounds keep it from matching
# a hyphenated id-shaped substring embedded inside a longer alphanumeric run
# (rather than matching only bracket-delimited ids, since the whole point is
# these tokens are NOT reliably bracket-delimited once they've leaked here).
#
# Zero-separator adjacency (adversarial review of this same backstop): a
# plain `(?<![0-9A-Za-z])`/`(?![0-9A-Za-z])` boundary treats a SECOND id
# butted directly against the first one, with no separator of any kind, as
# "embedded inside a longer alphanumeric run" - the same shape the guard
# above is deliberately protecting against for an ordinary word. That is
# wrong here: `S-1111111111S-2222222222` is two id-shaped tokens back to
# back, not one word, but the first id's trailing lookahead sees the
# second id's leading `S` (alphanumeric) and refuses to close, while the
# second id's leading lookbehind sees the first id's trailing `1`
# (alphanumeric) and refuses to open - each id fails to match because of
# the OTHER, and BOTH raw ids leak onto the page verbatim, unredacted, with
# no warning. Each boundary is therefore widened to accept not just "not
# alphanumeric" but also "immediately preceded/followed by another
# complete id-shaped token" (a second, fixed-width 12-character lookaround
# alternative - `[PSCLH]-[0-9a-hjkmnp-tv-z]{10}` is always exactly 12
# characters, so this stays a legal fixed-width Python lookbehind) - so two
# (or more) glued-together ids now redact individually in one left-to-right
# sweep, while a bare id embedded inside an ordinary longer word (this
# guard's original, still-intact purpose) is still correctly left alone.
_BARE_ID_RE = re.compile(
    r'(?:(?<![0-9A-Za-z])|(?<=[PSCLH]-[0-9a-hjkmnp-tv-z]{10}))'
    r'[PSCLH]-[0-9a-hjkmnp-tv-z]{10}'
    r'(?:(?![0-9A-Za-z])|(?=[PSCLH]-[0-9a-hjkmnp-tv-z]{10}))', re.I)

# Replacement text for a `_BARE_ID_RE` match (M8 UX bar, `_LIVING_LABEL`/
# `_RESTRICTED_LABEL`'s own rule: redacted content is NAMED, never silently
# dropped to a blank). `render_token`'s own "unresolved token" convention
# (`<mark>[X-xxxx]</mark>`, `render_token`'s final fallback) is NOT reused
# here on purpose - it still shows the raw id text, which is exactly what
# this backstop exists to stop leaking; a generic, non-identifying
# placeholder is used instead. Deliberately plain text, not markup:
# `_scrub_internal_encoding` returns plain text that its caller HTML-escapes
# afterward (`_inline_html`), so - unlike `render_token`'s HTML, built and
# returned directly, never re-escaped - anything but plain text here would
# come out as visible, broken `&lt;span&gt;`-style tags. The bracket
# punctuation mirrors `render_token`'s bracketed look without needing markup.
_BARE_ID_LABEL = '[record]'

# Sentinel passed as `_scrub_internal_encoding`'s `next_char` for the
# literal-text run immediately preceding a matched inline construct whose
# own first rendered character is NOT knowable without calling
# `render_token` (`_inline_html`, #167 finding 1; see its docstring for the
# full boundary-character picture) - any alphanumeric character works,
# `_strip_claim_id_paren`/`_redact_bare_id` only ever call `.isalnum()` on
# it. Its actual value never reaches rendered output; it exists purely to
# say "yes, a word character follows here with no gap" for a run whose OWN
# text happens to end exactly where the match begins.
_INLINE_BOUNDARY_CHAR = 'x'

# A raw `[..YYYY[-MM[-DD]]]` "before" date (SPEC §11's bracket form) embedded
# mid-sentence in prose (#140) - the same notation base.html's shared footer
# legend explains (#131) when it appears as a standalone structured date, but
# a reader meeting the literal brackets/dots inside a sentence gets no such
# context and the sentence reads as broken. Translated in place instead.
_DATE_BEFORE_RE = re.compile(r'\[\.\.(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?\]')

# The two-sided-interval shape `_translate_date_before` deliberately leaves
# alone (see its docstring): one bound written as a `[..YYYY[-MM[-DD]]]`
# bracket, the OTHER a plain `YYYY[-MM[-DD]]` with no bracket, joined by `/`
# (#167 finding 3). `(?<!\d)`/`(?!\d)` bound the plain side so it can't
# silently swallow (or be swallowed by) an adjacent digit run that isn't
# really part of this date - the bracket side needs no such guard since `[`
# and `]` are already unambiguous delimiters. Two full alternatives (rather
# than one pattern with the bracket "on either side") because Python `re`
# cannot reuse the same group name twice in one pattern; `by`/`bm`/`bd` name
# the bracketed year/month/day, `py`/`pm`/`pd` the plain ones, suffixed `1`
# for "bracket comes first" and `2` for "plain comes first".
#
# `pq1`/`pq2` (adversarial review of #167 finding 3) let the plain side also
# carry a trailing EDTF uncertainty/approximation qualifier - `?` or `~`,
# the only two this archive's EDTF dialect uses (`_lib._EDTF_PATTERNS`;
# `_lib.humanize_edtf`/`_humanize_edtf_bound` read them as "(unconfirmed)"
# and "about ..." respectively, see `_translate_date_before_slash`). Without
# this, a plain bound already valid per `is_valid_edtf` (which accepts a
# trailing `?`/`~` on the year/month/day component - `_EDTF_PATTERNS`) but
# carrying one, e.g. `[..1900]/1910?`, matched only the unqualified `1910`
# prefix: the `?` fell outside the match entirely and survived untranslated
# in reader-facing prose, and the mirrored `1900?/[..1910]` did not match at
# all, since the un-widened plain-side pattern could never reach past the
# `?` to find the `/` right after it. The qualifier is bound to the SAME
# `(?!\d)`/lookahead discipline as the rest of the plain side: `pq1` sits
# before the existing `(?!\d)` (so a stray digit still can't follow), and
# `pq2` sits directly before the literal `/` (the only thing allowed to
# follow the plain side in that alternative). The bracket side never carries
# a qualifier in this dialect (`_EDTF_PATTERNS`'s bracket form allows no
# `?`/`~` inside `[..]` at all), so only the plain side needs this.
#
# `pmq1`/`pdq1` (`pmq2`/`pdq2` for the plain-first alternative) extend this
# further (fresh Codex finding on the same #167 finding-3 pipeline, after the
# trailing-qualifier fix above shipped): this archive's EDTF dialect also
# lets a `~` sit COMPONENT-LEVEL, immediately before the month or day digits,
# rather than trailing the whole date - "1910-~06" reads "1910, approximately
# June": the year is certain, only the month is a guess
# (`_EDTF_PATTERNS`'s `~?` sitting directly before each `\d{2}` component).
# Unlike the trailing marker, `?` is never valid in this leading position -
# `_EDTF_PATTERNS` only ever writes `~?` there, never `[?~]?` - so `pmq`/`pdq`
# only ever capture a literal tilde. Before this, `[..1900]/1910-~06` matched
# only the unqualified `1910` prefix - the exact same bug `pq1`/`pq2` fixed
# above, but for the mid-date form instead of the trailing one - and the
# mirrored `1910-~06/[..1900]` did not match at all, for the identical
# reason `pq2` was needed: the plain side could not reach past `-~06` to
# find the `/`. A trailing `pq`/`pq2` marker and a leading `pmq`/`pdq` marker
# are not mutually exclusive in the grammar (`1910-~06?` is syntactically
# valid too), so both are captured independently rather than as alternatives;
# `_translate_date_before_slash` reconciles the two into one wording.
_DATE_BEFORE_SLASH_RE = re.compile(
    r'\[\.\.(?P<by1>\d{4})(?:-(?P<bm1>\d{2}))?(?:-(?P<bd1>\d{2}))?\]'
    r'/(?P<py1>\d{4})(?:-(?P<pmq1>~)?(?P<pm1>\d{2}))?'
    r'(?:-(?P<pdq1>~)?(?P<pd1>\d{2}))?(?P<pq1>[?~])?(?!\d)'
    r'|(?<!\d)(?P<py2>\d{4})(?:-(?P<pmq2>~)?(?P<pm2>\d{2}))?'
    r'(?:-(?P<pdq2>~)?(?P<pd2>\d{2}))?(?P<pq2>[?~])?'
    r'/\[\.\.(?P<by2>\d{4})(?:-(?P<bm2>\d{2}))?(?:-(?P<bd2>\d{2}))?\]',
)

_MONTH_NAMES = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
                'August', 'September', 'October', 'November', 'December')


def _strip_claim_id_paren(match: re.Match, boundary_next_char: str | None = None) -> str:
    """One `(C-xxxxxxxxxx)` match -> '' (dropped) or a single space (#144
    review finding 1; #167 finding 1 extends it to the block-level boundary).

    The regex eats an optional single LEADING space along with the
    parenthetical, so a trailing sentence like " (C-xxx)." collapses cleanly
    to "." with no orphan space. But that leading-space consumption has
    nothing to say about what follows the match: a hand-edited parenthetical
    sitting directly against the next word with no space of its own
    ("record(C-xxx)confirms") loses its ONLY separator and the two words run
    together ("recordconfirms"). Reinserting a single space whenever the
    character right after the match is an ordinary word character (not
    whitespace, not punctuation, not the end of the string) restores that
    separator without ever double-spacing the already-correct case where a
    space or punctuation mark already follows.

    `boundary_next_char` covers the one case this match's OWN string can't
    answer: `_scrub_internal_encoding` is called per literal-text SPAN
    inside `_inline_html`'s loop, so a claim-id-paren sitting at the very
    END of a span that is immediately followed - in the FULL original text -
    by a different matched inline construct (`**bold**`, a markdown link, a
    wikilink) has nothing after it within `match.string`; `end < len(text)`
    is false even though, in the real text, a word character effectively
    follows right away. `end == len(text)` is exactly the "this match is at
    the true end of whatever string it was given" case, so only then do we
    fall back to the caller-supplied boundary character - any match that
    has real trailing text within its own string (including an earlier
    claim-id-paren followed by more claim-id-parens) is unaffected and
    keeps deciding from its own `text[end]` as before."""
    end = match.end()
    text = match.string
    if end < len(text):
        next_char = text[end]
    else:
        next_char = boundary_next_char
    if next_char is not None and next_char.isalnum():
        return ' '
    return ''


def _redact_bare_id(match: re.Match, boundary_next_char: str | None = None) -> str:
    """One bare `[PSCLH]-xxxxxxxxxx` match -> `_BARE_ID_LABEL` (#167 finding
    2 continued - see `_BARE_ID_RE`'s docstring for why this backstop
    exists).

    Unlike `_strip_claim_id_paren`'s match, this one is never dropped to
    nothing - but `_BARE_ID_LABEL` still ends in a non-word glyph (`]`), so
    the exact same welding risk `_strip_claim_id_paren` guards against
    applies here too: a bare id sitting at the very END of a literal-text
    run that is immediately followed - in the FULL original text - by a
    different matched inline construct with no gap of its own would
    otherwise leave `_BARE_ID_LABEL`'s closing `]` jammed directly against
    the next word with no separating space
    ("...S-1111111111**note**" -> "...[record]note"). Same boundary logic
    as `_strip_claim_id_paren`: consult the match's own trailing text first,
    falling back to the caller-supplied `boundary_next_char` only when this
    match sits at the true end of whatever string it was given."""
    end = match.end()
    text = match.string
    if end < len(text):
        next_char = text[end]
    else:
        next_char = boundary_next_char
    if next_char is not None and next_char.isalnum():
        return _BARE_ID_LABEL + ' '
    return _BARE_ID_LABEL


def _format_edtf_ymd(year: str, month: str | None, day: str | None) -> str:
    """Plain English reading of one already-validated YYYY[-MM[-DD]]
    component - '1900', 'May 1900', or 'May 3, 1900'. Shared by
    `_translate_date_before` (a single bracket-qualified "before" bound) and
    `_translate_date_before_slash` (#167 finding 3, a two-sided interval
    with one bracketed bound), so both phrase a month/day the same way. Not
    to be confused with `_lib._humanize_edtf_bound`, which reads a raw EDTF
    token and uses "3 May 1900" day-before-month ordering with no comma -
    this keeps site.py's own pre-existing "May 3, 1900" wording, established
    by `_translate_date_before` before this helper was split out, so no
    already-shipped rendering changes shape."""
    if month:
        month_name = _MONTH_NAMES[int(month) - 1]
        if day:
            return f'{month_name} {int(day)}, {year}'
        return f'{month_name} {year}'
    return year


def _apply_edtf_qualifier(label: str, qualifier: str | None) -> str:
    """Wrap an already-`_format_edtf_ymd`-formatted label with this
    archive's EDTF qualifier wording (adversarial review of #167 finding 3):
    `?` (uncertain) -> '<label> (unconfirmed)', `~` (approximate) ->
    'about <label>'. These are the only two qualifier characters this
    archive's EDTF dialect recognises (`_lib._EDTF_PATTERNS`); `None` (no
    qualifier) returns `label` unchanged.

    Reuses `_lib._humanize_edtf_bound`'s existing wording verbatim rather
    than inventing new phrasing, but does NOT call that function directly -
    it reads a raw EDTF token and renders day-before-month with no comma
    ("3 May 1900"), while `_format_edtf_ymd` deliberately keeps site.py's
    own pre-existing "May 3, 1900" ordering (see its own docstring). This
    helper applies just the qualifier VOCABULARY on top of a label already
    formatted the site.py way, so the two date orderings never mix within
    one rendered phrase."""
    if qualifier == '?':
        return f'{label} (unconfirmed)'
    if qualifier == '~':
        return f'about {label}'
    return label


def _translate_date_before(match: re.Match) -> str:
    """One `[..YYYY[-MM[-DD]]]` match -> the plain phrase base.html's date-
    notation legend already uses for this form ("before <date>"), so prose
    and the legend never teach the reader two different words for the same
    mark.

    Two guards keep this from ever emitting a wrong or broken reading (#144
    review findings 2 and 5):
      - The matched year/month/day is validated as a real calendar date
        (via `_lib.is_valid_edtf`, the same validator `process.py`/`claim.py`
        use for `--date`) before any translation happens. A syntactically-
        matching but impossible bound - `[..1900-02-31]` (no such day),
        `[..1900-13-01]` (no such month) - is left exactly as written rather
        than rendered as a nonsensical "before February 31, 1900" or
        silently reduced to "before 1900" by just dropping the bad groups.
      - A bracket sitting directly against a `/` (`[..1900]/1910` or
        `1900/[..1910]`) is one bound of a two-sided EDTF interval, not a
        standalone "before" date, and this function only ever renders a
        single "before X" phrase - it has no way to also speak the OTHER
        side of the interval. `_scrub_internal_encoding` runs
        `_DATE_BEFORE_SLASH_RE`/`_translate_date_before_slash` FIRST (#167
        finding 3), which recognizes and fully translates the specific
        two-sided shape "one bracketed bound, one plain bound" - so by the
        time this function's own regex (`_DATE_BEFORE_RE`, which cannot see
        across the `/` at all) runs, that shape is already gone from the
        text. This guard therefore now only ever fires for the shapes the
        slash translator does NOT handle - chiefly `[..YYYY]/[..YYYY]` (both
        sides bracketed), which stays deliberately out of scope (see
        `_translate_date_before_slash`'s docstring) - and this bracket is
        still left exactly as written rather than guessed at.
    """
    text = match.string
    start, end = match.start(), match.end()
    if (start > 0 and text[start - 1] == '/') or (end < len(text) and text[end] == '/'):
        return match.group(0)
    year, month, day = match.group(1), match.group(2), match.group(3)
    bare = year
    if month:
        bare += f'-{month}'
        if day:
            bare += f'-{day}'
    if not is_valid_edtf(bare):
        return match.group(0)
    return f'before {_format_edtf_ymd(year, month, day)}'


def _translate_date_before_slash(match: re.Match) -> str:
    """One two-sided EDTF interval, exactly one side a `[..YYYY[-MM[-DD]]]`
    bracket and the other a plain `YYYY[-MM[-DD]]`, joined by `/` -> both
    bounds in plain English (#167 finding 3), e.g. `[..1900]/1910` ->
    "before 1900 to 1910" and `1900/[..1910]` -> "1900 to before 1910".

    Before this, `_translate_date_before`'s own adjacency guard (see its
    docstring) deliberately left the WHOLE interval untouched rather than
    translate only the bracketed half and leave the other half raw - safe,
    but incomplete: it never came back to finish the one shape it CAN render
    completely and correctly. That left raw `[..YYYY]/YYYY`-style notation
    on the reader-facing page for exactly the sentence the humanizer exists
    to fix. This function is `_scrub_internal_encoding`'s FIRST date-bracket
    pass (before `_DATE_BEFORE_RE`/`_translate_date_before`), so it sees -
    and consumes - the bracket together with its `/` and the other bound
    before the single-bracket translator or its "leave the whole interval
    alone" guard ever gets a chance to fire on the now-already-handled
    bracket.

    `[..YYYY]/[..YYYY]` (BOTH sides bracketed) is NOT matched here and
    stays out of scope, left untouched by `_translate_date_before`'s
    existing guard exactly as before this fix: `_lib.humanize_edtf` - the
    established model for this project's interval wording (a slash splits
    on `/`, humanizes each side, joins as "X to Y") - has no existing
    phrasing for a bracket on BOTH sides of a slash either, so this would be
    inventing vocabulary with no precedent anywhere in the codebase rather
    than reusing established wording; left as a clearly-flagged narrowing of
    this fix's coverage rather than guessed at.

    Each side is validated with `is_valid_edtf` exactly as
    `_translate_date_before` validates its single bound - either side
    failing validation (e.g. `[..1900-13-01]/1910`, no such month) leaves
    the WHOLE match untouched rather than translate one real bound next to
    one bogus one.

    The plain side may also carry a trailing `?`/`~` EDTF qualifier
    (adversarial review of #167 finding 3): `[..1900]/1910?` -> "before
    1900 to 1910 (unconfirmed)", `1900?/[..1910]` -> "1900 (unconfirmed) to
    before 1910" - see `_DATE_BEFORE_SLASH_RE`'s own comment for why the
    regex needed widening to consume it at all, and
    `_apply_edtf_qualifier` for the wording. Validation runs on the BARE
    component (qualifier stripped) exactly as `_translate_date_before`
    validates its single bound - the qualifier is a confidence marker, not
    part of calendar validity, so `[..1900]/1910-13-01?` (invalid month,
    qualifier or not) still leaves the WHOLE match - qualifier included -
    untouched via `match.group(0)`. The bracket side never carries a
    qualifier in this dialect (see `_DATE_BEFORE_SLASH_RE`'s comment), so
    only the plain side's label is ever wrapped.

    The plain side may ALSO carry a component-level `~` sitting before the
    month or day instead of trailing the whole date (fresh Codex finding on
    this same pipeline): `[..1900]/1910-~06` -> "before 1900 to about June
    1910", `1910-~06/[..1900]` -> "about June 1910 to before 1900". This is
    folded into the same wrap `_apply_edtf_qualifier` already does for a
    trailing marker rather than given its own phrasing - `_lib.
    _humanize_edtf_bound` already treats a component-level `~` exactly like
    a trailing one (it only ever checks "is `~` present anywhere in this
    token", never WHERE), so there is no established wording to invent here.
    A trailing `?`/`~` and a component-level `~` are not mutually exclusive
    in the grammar (`1910-~06?` is valid too); when both are present the
    trailing `?` wins - the same "uncertain beats approximate" precedence
    `_humanize_edtf_bound` already applies for a component carrying both
    markers at once. Calendar validation still runs on the fully bare
    component (both the leading and trailing markers stripped), so a
    component-level `~` next to a genuinely impossible month/day - e.g.
    `[..1900]/1910-~13` (no month 13) - still leaves the WHOLE match
    untouched, exactly like the trailing-qualifier case above."""
    g = match.groupdict()
    bracket_first = g['by1'] is not None
    if bracket_first:
        b_year, b_month, b_day = g['by1'], g['bm1'], g['bd1']
        p_year, p_month, p_day = g['py1'], g['pm1'], g['pd1']
        p_trailing_qualifier = g['pq1']
        p_has_component_approx = g['pmq1'] is not None or g['pdq1'] is not None
    else:
        p_year, p_month, p_day = g['py2'], g['pm2'], g['pd2']
        b_year, b_month, b_day = g['by2'], g['bm2'], g['bd2']
        p_trailing_qualifier = g['pq2']
        p_has_component_approx = g['pmq2'] is not None or g['pdq2'] is not None

    def _bare(year: str, month: str | None, day: str | None) -> str:
        v = year
        if month:
            v += f'-{month}'
            if day:
                v += f'-{day}'
        return v

    if not is_valid_edtf(_bare(b_year, b_month, b_day)) or \
            not is_valid_edtf(_bare(p_year, p_month, p_day)):
        return match.group(0)

    # Reconcile a trailing `?`/`~` with a leading component-level `~`
    # (`pmq`/`pdq`) into the single qualifier `_apply_edtf_qualifier`
    # already knows how to wrap - the two are not mutually exclusive in the
    # grammar (`1910-~06?` is valid), so a bare "is either present" check
    # would lose whichever the wording actually needs. `_lib.
    # _humanize_edtf_bound` prefers "not sure at all" (`?`) over "roughly
    # right" (`~`) when one component carries both; this mirrors that same
    # precedence rather than inventing a new rule.
    p_qualifier = p_trailing_qualifier
    if p_qualifier != '?' and p_has_component_approx:
        p_qualifier = '~'

    before_label = f'before {_format_edtf_ymd(b_year, b_month, b_day)}'
    plain_label = _apply_edtf_qualifier(
        _format_edtf_ymd(p_year, p_month, p_day), p_qualifier)
    if bracket_first:
        return f'{before_label} to {plain_label}'
    return f'{plain_label} to {before_label}'


def _scrub_internal_encoding(text: str, next_char: str | None = None) -> str:
    """Remove/translate internal-only encoding that must never reach reader-
    facing prose (#140): a bare claim-id parenthetical is dropped outright
    (spacing preserved, #144 finding 1; boundary-aware, #167 finding 1), a
    bare `[PSCLH]-xxxxxxxxxx`-shaped citation id that reached here unlinked
    is replaced with a generic placeholder (boundary-aware the same way -
    #167 finding 2 continued, see `_BARE_ID_RE`'s docstring), a raw
    `[..YYYY]`-shaped "before" date is translated to plain English when it
    validates as a real date and stands alone (not one bound of a `/`
    interval - #144 findings 2 and 5), and a two-sided interval with exactly
    one bracketed bound is translated in full - both bounds in plain
    English (#167 finding 3). Applied to raw value/notes/body text BEFORE
    any HTML escaping, so callers doing their own index-based substitutions
    afterward (e.g. the timeline's place-mention span) see only the
    already-scrubbed text.

    The claim-id-paren pass runs BEFORE the bare-id pass deliberately: a
    well-formed `(C-xxxxxxxxxx)` parenthetical must be fully consumed - and
    its own smart-spacing logic applied - as ONE unit first, or the bare-id
    pass would rewrite the id INSIDE the parens first (`(C-xxx)` ->
    `([record])`), and `_CLAIM_ID_PAREN_RE` would then no longer recognize
    the now-different text as its own pattern at all, silently losing the
    drop-the-whole-parenthetical behavior #144/#167 finding 1 established.

    `next_char`, when given, is the character that immediately follows
    `text` in the CALLER's full original string (not necessarily reflected
    anywhere inside `text` itself) - see `_strip_claim_id_paren`'s docstring
    for why `_inline_html` needs to pass this for a literal-text run that
    ends exactly where a different matched inline construct begins."""
    if not text:
        return text
    text = _CLAIM_ID_PAREN_RE.sub(lambda m: _strip_claim_id_paren(m, next_char), text)
    text = _BARE_ID_RE.sub(lambda m: _redact_bare_id(m, next_char), text)
    text = _DATE_BEFORE_SLASH_RE.sub(_translate_date_before_slash, text)
    text = _DATE_BEFORE_RE.sub(_translate_date_before, text)
    return text


def _prose_to_html(text: str, render_token, render_embed=None, *, drop_private: bool = False) -> str:
    """Convert a simple markdown block to HTML using only the stdlib.

    The profile prose format is deliberately simple (TOOLING §12: "headings,
    bold, lists, links"), so a full markdown library is unwarranted. We split
    on blank lines into blocks; a block is a heading, a bullet list, or a
    paragraph. Inline formatting (links, tokens, bold) is applied per line via
    `_inline_html`. Headings below the page H1 render as `<h3>` so they sit
    under the section's own `<h2>` ("Biography", "Stories") without competing
    with it.

    `drop_private=True` (a public/standalone build) strips `<!-- private -->…
    <!-- /private -->` fenced prose before rendering; the linked preview keeps
    the content and only removes the marker comments.

    Internal-only encoding (`_scrub_internal_encoding`, #140) is NOT applied
    here as a pre-pass over the raw block - it runs inside `_inline_html`
    instead, per literal-text span, after markdown links/tokens/bold are
    already identified (#144 finding 3; see that function's docstring for
    why a whole-block pre-pass corrupts a link target that happens to
    contain a claim-id- or date-bracket-shaped substring).
    """
    if text:
        text = apply_private_fence(text, drop=drop_private)
    if not text or not text.strip():
        return ''
    lines = text.replace('\r\n', '\n').split('\n')
    blocks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if render_embed is not None:
            emb = _EMBED_RE.match(line)
            if emb:
                fig = render_embed(emb.group(1).strip(), (emb.group(2) or '').strip())
                if fig:
                    blocks.append(fig)
                i += 1
                continue
        heading = _HEADING_RE.match(line)
        if heading:
            blocks.append(f'<h3>{_inline_html(heading.group(2).strip(), render_token)}</h3>')
            i += 1
            continue
        if _LIST_RE.match(line):
            items: list[str] = []
            while i < n and _LIST_RE.match(lines[i]):
                items.append(
                    f'<li>{_inline_html(_LIST_RE.match(lines[i]).group(1).strip(), render_token)}</li>'
                )
                i += 1
            blocks.append('<ul>' + ''.join(items) + '</ul>')
            continue
        # Paragraph: gather consecutive non-blank, non-heading, non-list lines.
        para: list[str] = []
        while i < n and lines[i].strip() and not _HEADING_RE.match(lines[i]) and not _LIST_RE.match(lines[i]):
            para.append(lines[i].strip())
            i += 1
        blocks.append(f'<p>{_inline_html(" ".join(para), render_token)}</p>')
    return '\n'.join(blocks)


def _extract_section(body: str, heading: str) -> str | None:
    """Return the body of a `## {heading}` section, or None.

    Used for the biography prose, which lives in the person `.md` body, not the
    index (TOOLING §12). The placeholder `*(none yet)*` reads as empty so a
    skeleton section never renders an empty card.
    """
    pat = re.compile(
        r'^##\s+' + re.escape(heading) + r'\s*\r?\n(.*?)(?=^## |\Z)',
        re.S | re.M,
    )
    m = pat.search(body)
    if not m:
        return None
    content = m.group(1).strip()
    if not content or content in ('*(none yet)*', '(none yet)'):
        return None
    return content


def _question_block_body(block: str) -> str:
    """A `## Q:` block's body: its own heading line dropped, cut before the
    next markdown heading of any level (issue #117).

    `_lib.parse_question_blocks` splits purely on `## Q:` boundaries (see its
    own docstring): fed a WHOLE research file, as `_lib.parse_questions` does
    for a person's own '## Open Questions' section, the LAST question block
    in that file keeps everything that follows it too - `## Hypotheses`,
    `## Research Log`, whatever the SPEC §16 scaffold puts next - because
    nothing in that split stops at a heading of any OTHER level. That reach
    never mattered before: the only existing reader of a block's raw text
    (report.py's answerable-questions section) only keyword-searches it. A
    person's page RENDERS it, so an untrimmed block would show someone's
    Hypotheses and Research Log as if they were part of an unrelated open
    question. This stops at the same boundary `_extract_section` respects
    for every other person-page section - the next `## ` heading, of any
    kind - and drops the block's own heading line, since the template shows
    that heading text on its own.
    """
    _first_line, _sep, rest = block.partition('\n')
    m = re.search(r'^## ', rest, re.M)
    return rest[:m.start()] if m else rest


# ── Dates ───────────────────────────────────────────────────────────────────

def _decade_header(date_edtf: str | None) -> str | None:
    """EDTF date → '1880s' decade label, or None when undated.

    Mirrors views.py `_decade_from_edtf`: read the decade from the *display*
    EDTF, not from the widened date_min (an approximate '1840~' has date_min
    '1839-01-01' and would land in the wrong decade). Duplicated rather than
    imported - tools never import tools (TOOLING §15).
    """
    if not date_edtf:
        return None
    edtf = date_edtf.split('/')[0].strip().lstrip('[.').rstrip('~?]')
    if len(edtf) >= 4 and edtf[:3].isdigit() and edtf[3] in ('X', 'x'):
        return f'{int(edtf[:3]) * 10}s'
    try:
        return f'{(int(edtf[:4]) // 10) * 10}s'
    except (ValueError, IndexError):
        return None


# ── Places ──────────────────────────────────────────────────────────────────

# A place label's leading hierarchical component (#127 reopened) is bounded
# by its first top-level COMMA only - "Millbrook" out of "Millbrook, Dutchess
# County, New York". Any parenthetical is elaboration (a nested list of
# narrower places), never a new hierarchy level, so it is stripped before
# that comma search runs: "Rome, Milan, Paris, Lyon" inside "Italy and
# France (Rome, Milan, Paris, Lyon)" must never be mistaken for the label's
# OWN top-level comma. See `_place_leading_parts` for what happens to the
# word "and" inside that leading component.
_PLACE_PARENTHETICAL_RE = re.compile(r'\([^()]*\)')
_PLACE_AND_RE = re.compile(r'\band\b', re.IGNORECASE)


def _place_leading_component(place_label: str) -> str:
    """`place_label`'s leading hierarchical component - the text before its
    first top-level comma (parentheticals stripped first) - or the label
    unchanged when it has no top-level comma at all. See
    `_place_leading_parts` and `_place_mention_span`."""
    label = place_label or ''
    without_parens = _PLACE_PARENTHETICAL_RE.sub('', label)
    comma = without_parens.find(',')
    leading = without_parens[:comma] if comma != -1 else without_parens
    return leading.strip()


def _place_leading_parts(place_label: str) -> list[str]:
    """The name(s) that must ALL be found - as one coordinated expression,
    not merely anywhere independently (#127 reopened, finding 2 follow-up;
    see `_match_coordinated_place_parts`) - before the leading component
    counts as "already stated" (#127 reopened, adversarial-review
    follow-up).

    Comma marks a HIERARCHY in this codebase's free-text `place_text`
    convention (city, county, state - each broader than the last), so
    `_place_leading_component` already cuts there: dropping the broader
    units only loses precision, never identity, so a match on the leading
    piece alone is enough.

    "and" inside that leading component is different - it marks
    COORDINATION, not hierarchy: two peer entities forming one compound name
    ("Trinidad and Tobago", "Bosnia and Herzegovina") or a genuine list of
    two different places ("Italy and France"). Matching just the first of
    those and suppressing the whole tag does not lose precision, it changes
    what place is actually named - "born in Trinidad" is not the same fact
    as "born in Trinidad and Tobago" (there is also a Trinidad, Colorado and
    a Trinidad, Cuba). So every coordinate part returned here must appear
    together, in order, as the label's own "part1 ... and ... part2"
    expression (`_match_coordinated_place_parts`) before suppression is
    safe; two parts scattered across unrelated clauses of the sentence do
    not count, and neither does one part missing outright - the full
    trailing tag still has to print rather than silently drop (or
    misattribute) the part the sentence never actually named as one place.

    "Italy and France (Rome, Milan, Paris, Lyon)" still resolves to
    `["Italy", "France"]` - the parenthetical never reaches this function at
    all, having already been stripped out in `_place_leading_component`."""
    leading = _place_leading_component(place_label)
    if not leading:
        return []
    if not _PLACE_AND_RE.search(leading):
        return [leading]
    parts = [p.strip() for p in _PLACE_AND_RE.split(leading)]
    return [p for p in parts if p]


def _match_place_words(value: str, label: str) -> tuple[int, int] | None:
    """Whole-word, loose-punctuation search for `label`'s words in `value`
    (the matching rules `_place_mention_span` documents); span in `value`'s
    coordinates, or None."""
    words = re.findall(r'\w+', label or '')
    if not words or not value:
        return None
    pattern = r'\b' + r'\W+'.join(re.escape(w) for w in words) + r'\b'
    match = re.search(pattern, value, re.IGNORECASE)
    return match.span() if match else None


# A coordinated pair's connective, permissive enough to bridge the shapes
# already known to occur between two coordinate place names - an elaborating
# parenthetical sitting right against the "and" ("Italy (Rome, Milan) and
# France (Paris, Lyon)"), and an Oxford-style comma immediately before it
# ("Trinidad, and Tobago") - but nothing looser than that: no unrelated
# clause, no second sentence, may sit between the two names. This is what
# makes "part1 ... and ... part2" a genuinely bounded, coordinated mention
# rather than "part1 appears somewhere, part2 appears somewhere else" (#127
# reopened, finding 2 follow-up).
#
# The optional comma closes a real regression (adversarial review, round 4
# audit): `_match_place_words` - the WHOLE-label matcher `_place_mention_span`
# always tries first - joins a label's words with a loose `\W+`, so "Trinidad,
# and Tobago" already matched a bare "Trinidad and Tobago" label outright,
# never even reaching this function. But the identical sentence, matched
# against a label carrying a trailing qualifier ("Trinidad and Tobago,
# Caribbean" - the whole-label match fails on the un-stated ", Caribbean",
# falling through to THIS coordinated-parts path), used to be silently
# rejected: the ordinary, everyday comma-before-"and" phrasing that already
# worked for the simpler label shape stopped working the moment a qualifier
# was added, with no user-visible reason why - the sentence still repeated
# the compound name in full instead of suppressing it. The comma allowed
# here is exactly as narrow as the parenthetical already was: right against
# "and", nothing else permitted around it, so a real clause boundary
# (semicolon, "a witness later traveled to") still correctly fails to match.
_PLACE_AND_GAP_RE = r'(?:\s*\([^()]*\))?\s*,?\s*\band\b\s*(?:\([^()]*\))?\s*'


def _match_coordinated_place_parts(value: str, parts: list[str]) -> tuple[int, int] | None:
    """Whether `parts` (>= 2 coordinate names from `_place_leading_parts`,
    e.g. `["Trinidad", "Tobago"]`) appear in `value` as ONE coordinated
    mention - "part1 ... and ... part2 ..." in that order, each part's own
    words matched whole-word/loose-punctuation exactly as
    `_match_place_words` does - rather than each part merely appearing
    somewhere in the sentence with no relationship to the other (#127
    reopened, finding 2 follow-up).

    Two independent `_match_place_words` calls satisfied Codex's adversarial
    example - "Born in Trinidad; a witness later traveled to Tobago" finds
    both "Trinidad" and "Tobago", even though the sentence never once names
    the compound country "Trinidad and Tobago" as the event's place, only
    two unrelated islands in two unrelated clauses. Requiring the label's
    OWN connective word ("and") to actually sit between the two matched
    names - permitting only the parenthetical elaboration the WORKING
    compound-list case already relies on (`_PLACE_AND_GAP_RE`) - rules that
    out while still matching "Italy (Rome, Milan) and France (Paris, Lyon)".

    Returns the span of the FIRST part's own words (same convention
    `_place_mention_span` has always used for hanging the place-page link),
    or None when no such coordinated mention exists."""
    word_patterns = []
    for part in parts:
        words = re.findall(r'\w+', part or '')
        if not words:
            return None
        word_patterns.append(r'\b' + r'\W+'.join(re.escape(w) for w in words) + r'\b')
    pattern = _PLACE_AND_GAP_RE.join(f'({p})' for p in word_patterns)
    match = re.search(pattern, value, re.IGNORECASE)
    return match.span(1) if match else None


def _place_mention_span(value: str, place_label: str) -> tuple[int, int] | None:
    """Where `value` already names `place_label` in its own words, or None.

    The person timeline prints the claim's sentence and then the claim's
    place, so a sentence that already says where it happened ("moved to
    Millbrook to farm") used to read "... to farm @ Millbrook" (#127), then
    "... to farm at Millbrook, Dutchess County, New York" once that first fix
    shipped (#127 reopened) - only the connector word had changed; the reader
    still saw "Millbrook" twice, once in their own sentence and once tacked
    on after it. Deciding what counts as "already named" needs more than
    `label in value`:

      - Whole words only. "Hampton" is a substring of "Southampton", and a
        plain containment test would read "married at Southampton" as already
        naming the place Hampton and drop a real, different place from the
        page. Losing a fact is worse than repeating one.
      - Punctuation and spacing between the words are matched loosely,
        because one place gets written both ways in practice: the registry
        says "Millbrook, NY" and the sentence says "Millbrook NY".

    A match against the WHOLE label is tried first - unchanged from #127's
    original fix, and still the only test a single-component label (no
    comma, no "and") ever gets. When that fails, a second try uses only the
    label's LEADING component (`_place_leading_component`): "Millbrook" out
    of "Millbrook, Dutchess County, New York", or "Italy" and "France" out
    of "Italy and France (Rome, Milan, Paris, Lyon)" (see
    `_place_leading_parts`). This is what #127 reopened changes - a sentence
    naming just that leading, reader-recognizable name now counts as already
    naming the place, even when a trailing qualifier (a county, a state)
    never gets restated in the tag. Dropping that qualifier from the
    timeline SENTENCE is no longer a dropped FACT (finding 1 follow-up):
    the matched words carry the place-page link when the place is
    registered and linkable (below), and when it is not,
    `_place_trailing_remainder` prints the qualifier itself as a plain
    continuation of the sentence instead of a duplicate "at Placename" -
    see that function for why a free-text `place_text` label's own first
    comma is a reliable enough boundary to carve a "leading name" from a
    "droppable qualifier" after all, contrary to this fix's own earlier,
    now-superseded call that no such boundary could be trusted.

    That comma-hierarchy handling breaks down, though, when the leading
    component itself coordinates two peer names with "and" - "Trinidad and
    Tobago", "Bosnia and Herzegovina" - rather than naming one place with a
    trailing qualifier (adversarial-review follow-up to #127 reopened). A
    sentence that says only "born in Trinidad" does not already name
    "Trinidad and Tobago" - Trinidad and Tobago is one specific country, and
    "Trinidad" alone is a different, ambiguous place (there is also a
    Trinidad, Colorado and a Trinidad, Cuba). Suppressing the tag there
    would not trim a qualifier, it would silently rewrite the archive's own
    recorded country into a different one. So when the leading component
    contains "and", EVERY coordinate part it names (`_place_leading_parts`)
    must appear together as one coordinated "part1 ... and ... part2"
    mention (`_match_coordinated_place_parts`, finding 2 follow-up) before
    suppression is safe - "Italy and France (Rome, Milan, Paris, Lyon)"
    only suppresses when the sentence names both "Italy" and "France" as
    that one coordinated expression, not when the two names merely occur
    somewhere, anywhere, in the sentence's unrelated clauses (Codex's
    adversarial example: "Born in Trinidad; a witness later traveled to
    Tobago" - two independent mentions of two different, unrelated facts,
    not one mention of the compound country). If the coordinated mention
    is not found at all, the full trailing tag still prints; printing a
    qualifier the reader can already half-guess is a far smaller cost than
    silently dropping - or misattributing - half of a two-part place name.
    A coordinated compound also has no natural "remainder" the way a
    hierarchy does (there is no sensible "born in Trinidad, and Tobago" the
    way there is a "moved to Millbrook, Dutchess County, New York"), so
    this shape is always either fully suppressed or fully printed, never
    given a remainder - see `_place_trailing_remainder`.

    Returning the span rather than a bare yes/no is what lets the caller hang
    the place-page link on the words already in the sentence instead of
    losing that link along with the repeated place name. When the leading
    component coordinates several parts, the link hangs on the first one -
    the same span this function has always returned for a leading-component
    match.
    """
    match = _match_place_words(value, place_label)
    if match is not None:
        return match
    parts = _place_leading_parts(place_label or '')
    if not parts or parts == [(place_label or '').strip()]:
        return None
    if len(parts) == 1:
        return _match_place_words(value, parts[0])
    return _match_coordinated_place_parts(value, parts)


def _place_trailing_remainder(value: str, place_label: str,
                               mention: tuple[int, int] | None, linkable: bool) -> str:
    """The text to print as a sentence CONTINUATION in place of the
    fully-suppressed trailing place tag, when suppressing the whole tag
    would otherwise erase real information the reader has no other way to
    recover (#127 reopened, finding 1 follow-up).

    `_place_mention_span` suppresses the trailing tag once a sentence
    already names a place's leading component ("Millbrook" out of
    "Millbrook, Dutchess County, New York") - safe when the place is
    registered and `linkable`, because the fuller label is one click away
    on the place's own page (`_timeline_value_html` hangs that link on the
    matched words). But most one-off residence/travel claims never get a
    place formally registered (SPEC.md), so for an UNLINKABLE claim, full
    suppression does not trim a qualifier - it deletes "Dutchess County,
    New York" from the archive's own record with no link anywhere to get
    it back. Printing "..., Dutchess County, New York" is the smaller
    cost: the reader's own sentence still doesn't repeat "Millbrook", and
    the county/state the record actually contains stays on the page.

    This only ever fires for the comma-HIERARCHY shape. `place_label`'s
    "leading name + droppable qualifier" structure only exists once its
    first top-level comma has been found (`_place_leading_component`) - the
    complementary remainder is everything from that comma onward,
    parentheticals stripped the same way the leading component strips them
    (so a coordination's own elaboration list, e.g. "(Rome, Milan, Paris,
    Lyon)", is never mistaken for a hierarchy's trailing qualifier). A
    label whose leading component instead coordinates peer names with
    "and" ("Trinidad and Tobago") has no such natural remainder - you
    cannot sensibly continue a sentence with "born in Trinidad, and
    Tobago" the way you can continue one with "moved to Millbrook,
    Dutchess County, New York" - so that shape keeps the prior commit's
    all-or-nothing behavior unchanged: fully suppressed when the whole
    coordinated expression is found, fully printed otherwise.

    Returns:
      - '' when there is nothing to add: no mention at all (the caller
        prints the full tag), the place IS linkable (the old behavior:
        full suppression, fuller name one click away), the sentence
        already stated the WHOLE label verbatim (nothing left to add), or
        the leading component coordinates peer names with "and" (no clean
        remainder for that shape - see above).
      - the label's own remainder text, comma included (e.g. ", Dutchess
        County, New York"), for an unlinkable claim whose sentence names
        only the label's leading hierarchical component."""
    if mention is None or linkable:
        return ''
    if _match_place_words(value, place_label) is not None:
        return ''   # the whole label was already stated verbatim
    if len(_place_leading_parts(place_label or '')) != 1:
        return ''   # "and"-coordination: no clean remainder for this shape
    without_parens = _PLACE_PARENTHETICAL_RE.sub('', place_label or '')
    comma = without_parens.find(',')
    return without_parens[comma:].strip() if comma != -1 else ''


# ── Image derivatives ─────────────────────────────────────────────────────────

def _make_derivative(src: Path, dest: Path, max_px: int = _DERIVATIVE_MAX_PX) -> bool:
    """Write a resized, EXIF-stripped copy of `src` to `dest`. True on success.

    Standalone snapshots must carry their own image derivatives so no full-res
    original - and none of its EXIF (camera, GPS, timestamps that could leak a
    living person's location) - ever leaves the archive (TOOLING §12). PIL drops
    metadata on a plain save; we additionally cap the longest edge (`max_px`,
    1200px by default; smaller for profile thumbnails).

    Failure (a corrupt image, an unsupported format, a locked file) returns
    False so the caller can warn-and-continue per the M8 UX bar (c) rather than
    abort the whole build. Caller must ensure PIL is available before calling.
    """
    try:
        with Image.open(src) as im:
            im = im.convert('RGB') if im.mode not in ('RGB', 'L') else im
            im.thumbnail((max_px, max_px))
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Re-save without the original info dict, so no EXIF/GPS survives.
            im.save(dest)
        return True
    except Exception:
        return False


# ── Ancestor fan chart (static SVG) ─────────────────────────────────────────

def _render_fan_svg(labels: dict, max_gen: int, r0: float = 54, ring: float = 60) -> str:
    """Render an ancestor fan as a self-contained, print-friendly SVG string.

    `labels` is an Ahnentafel map {number: {'name', 'url', 'redacted'}} - number 1
    is the subject, 2/3 the parents, 4-7 the grandparents, and so on. The fan is a
    180° semicircle with the subject at the hub; each segment's fill is a branch
    colour lightened by generation (set inline as CSS vars, composed by the
    stylesheet, so custom.css can retint the whole chart). Labels ride an SVG
    <textPath> - curved along the ring on the roomy inner generations, radial
    (reading outward) on the narrow outer ones - each shrunk to fit its own arc
    and shortened only once that shrink hits a readable floor. The chosen size
    goes out as a font-size presentation attribute, so styles.css must not
    declare a font-size on `.fan-label` itself: a rule matching the label beats
    a presentation attribute and flattens every label back to one size (#116).
    Colour/type come from the design tokens; this function only lays out geometry."""
    # Size to the actual depth present, not the configured maximum, so a shallow
    # tree renders as a small tidy fan rather than a huge mostly-empty canvas.
    n = min(max_gen, max((num.bit_length() - 1 for num in labels), default=1)) or 1
    r_max = r0 + n * ring
    pad = 14
    cx = r_max + pad
    cy = r_max + pad
    w = 2 * r_max + 2 * pad
    h = r_max + pad + 40

    def polar(r: float, a: float) -> str:
        return f"{cx + r * math.cos(a):.1f},{cy - r * math.sin(a):.1f}"

    defs: list[str] = []      # one <path> per label; text rides it via <textPath>
    body: list[str] = []
    lid = 0

    for num in sorted(labels):
        g = num.bit_length() - 1
        if g < 1 or g > n:
            continue
        info = labels[num]
        slot = num - (1 << g)
        seg = math.pi / (1 << g)
        a2 = math.pi - slot * seg
        a1 = math.pi - (slot + 1) * seg
        r_in = r0 + (g - 1) * ring
        r_out = r0 + g * ring
        # In this coord system (y flipped) increasing angle is counter-clockwise,
        # so the outer arc (a1→a2) sweeps 0 and the inner arc (a2→a1) sweeps 1;
        # this makes adjacent segments tile with no gaps.
        d = (f"M{polar(r_out, a1)} A{r_out:.1f},{r_out:.1f} 0 0 0 {polar(r_out, a2)} "
             f"L{polar(r_in, a2)} A{r_in:.1f},{r_in:.1f} 0 0 1 {polar(r_in, a1)} Z")
        if g >= 2:
            gp = num >> (g - 2)                       # gen-2 ancestor → grandparent line
            style = f'--seg-color:var(--branch-{(gp - 4) % 7 + 1}); --gen-fade:{min((g - 2) * 15, 55)}%'
        else:
            style = '--seg-color:var(--surface-sunken); --gen-fade:0%'
        body.append(f'<path class="fan-seg" style="{style}" d="{d}"/>')

        if info.get('redacted') or not info.get('name'):
            continue
        # Labels ride an invisible path (SVG textPath), so they curve along the ring
        # on the roomy inner generations and read radially (outward) on the narrow
        # outer ones - and never clip at a wedge edge. The path is drawn in the
        # direction that keeps text upright on this (upper) half of the circle.
        lid += 1
        pid = f'fan{lid}'
        mid = (a1 + a2) / 2
        rm = (r_in + r_out) / 2
        fs_max = (14, 13, 12, 11, 10)[min(g, 4)]
        if g < 4:                                     # inner: curved along the ring
            path_d = f"M{polar(rm, a2)} A{rm:.1f},{rm:.1f} 0 0 1 {polar(rm, a1)}"
            avail = rm * seg
        else:                                         # outer: radial line, outward-upright
            path_d = (f"M{polar(r_out, mid)} L{polar(r_in, mid)}" if mid > math.pi / 2
                      else f"M{polar(r_in, mid)} L{polar(r_out, mid)}")
            avail = r_out - r_in
        defs.append(f'<path id="{pid}" d="{path_d}"/>')
        full = info['name']
        # Shrink the label to fit the whole name on the arc, down to a readable
        # floor; only below the floor do we truncate (the roomy inner rings then
        # show full names, the tight outer rings shorten but keep it in the tooltip).
        _CW = 0.66                                    # approx glyph width in em for the serif
        _FS_MIN = 8.0                                 # readable floor; below it we shorten instead
        fs = min(fs_max, avail / (max(1, len(full)) * _CW))
        # Round the size DOWN to the one decimal the attribute is written with,
        # so the size drawn is never larger than the size the fit was measured
        # at - the whole point of #116 is that computed and rendered agree.
        fs = math.floor(fs * 10) / 10
        if fs >= _FS_MIN:
            # The size was picked so the whole name fits, so it fits. Deriving a
            # character budget back out of `fs` here only reintroduced the float
            # rounding it came from, and ellipsised names the shrink had already
            # made room for ("Chastina Augusta Re…" at 9.5px on a 21-character arc).
            name = full
        else:
            fs = _FS_MIN
            budget = max(3, int(avail / (fs * _CW)))
            name = full if len(full) <= budget else full[:budget - 1].rstrip() + '…'
        # The full name rides a <title> so a truncated arc label is never lossy:
        # hovering (or a screen reader) gives the whole name.
        title = f'<title>{html.escape(full)}</title>'
        label = (f'<text class="fan-label" font-size="{fs:.1f}"><textPath href="#{pid}" '
                 f'startOffset="50%">{html.escape(name)}</textPath></text>')
        url = info.get('url')
        body.append(f'<a class="fan-link" href="{html.escape(url, quote=True)}">{title}{label}</a>'
                    if url else f'<g>{title}{label}</g>')

    # subject: filled upper half-disk at the hub (left→right, sweep 1 arcs over
    # the top, so the hub fills the inner fan rather than hanging below) + name
    body.append(f'<path class="fan-seg fan-seg-subject" d="M{polar(r0, math.pi)} '
                f'A{r0:.1f},{r0:.1f} 0 0 1 {polar(r0, 0.0)} Z"/>')
    subj_full = labels.get(1, {}).get('name', '')
    if subj_full:
        # The hub is small and the page is already titled with the full name, so the
        # centre shows just the given name (full name in the tooltip) - no overflow.
        parts = subj_full.split()
        given = parts[0] if parts else subj_full
        if len(given) > 12:
            given = given[:11] + '…'
        body.append(f'<g><title>{html.escape(subj_full)}</title>'
                    f'<text class="fan-label-subject" x="{cx:.1f}" y="{cy - r0 * 0.42:.1f}">'
                    f'{html.escape(given)}</text></g>')

    out = [f'<svg class="fan-chart" viewBox="0 0 {w:.0f} {h:.0f}" '
           f'preserveAspectRatio="xMidYMid meet" role="img" aria-label="Ancestor fan chart">']
    if defs:
        out.append('<defs>' + ''.join(defs) + '</defs>')
    out += body
    out.append('</svg>')
    return '\n'.join(out)


def _branch_root(num: int) -> int:
    """The generation-1 slot (2 or 3) that Ahnentafel slot `num` reduces to -
    repeatedly halve (integer floor division, the same operation that walks a
    slot to its own parent) until it lands on 2 or 3. Split out of
    `_ancestor_branch` (#152 review fix, P2) so a caller can also ask "was
    THIS slot's own root actually sex-derived" before trusting the branch it
    implies - see `_ancestor_branch`'s docstring."""
    while num > 3:
        num //= 2
    return num


def _ancestor_branch(num: int) -> int:
    """1 (paternal, through slot 2) or 2 (maternal, through slot 3) for an
    Ahnentafel ancestor slot (#115 branch coloring, home pedigree only).

    An ancestor's ultimate line is recoverable from its OWN slot number alone,
    with no extra data or lookup: repeatedly halve the slot until it lands on
    2 or 3 - father's or mother's line (`_branch_root`). Only ever called for
    slot >= 2 (the subject, slot 1, has no line of its own to belong to).

    This is a pure function of the slot number - it does NOT know whether
    slot 2 vs 3 was actually DERIVED from a recorded `sex:` or merely
    DEFAULTED by elimination among unknown-sex parents (`_build_ahnentafel`'s
    `sex_derived` flag). `_render_pedigree_svg`'s card loop checks that flag
    itself (via `_branch_root` + the labels dict) before calling this
    function at all, and skips the branch color entirely for a defaulted
    root - see its own comment. A caller that wants a color with no evidence
    behind it can still call this directly; the render loop is the one place
    that must not."""
    return 1 if _branch_root(num) == 2 else 2


def _render_pedigree_svg(labels: dict, spouses: list[dict] | None = None,
                          children: list[dict] | None = None,
                          missing_parent_of: dict[int, str] | None = None,
                          workbench: bool = False,
                          siblings: list[dict] | None = None,
                          ancestor_generations: int = 2,
                          branch_color: bool = False,
                          axis_label: str | None = None,
                          home: bool = False) -> str:
    """Render a horizontal (left→right) family pedigree as a self-contained SVG.

    `labels` is an Ahnentafel map {number: {'name','url','redacted','dates'}} covering
    `ancestor_generations` generations up - slot 1 the subject, 2/3 the parents,
    4-7 the grandparents, and so on for as many generations as the caller asks
    for (see `_build_ahnentafel`, called with max_gen >= ancestor_generations).
    The person-page caller keeps the original default of 2 (parents +
    grandparents); the home pedigree (#115) calls with a deeper value (5 by
    default - `site.home_pedigree_generations`). `spouses`/`children` are the
    win-1 family-chart extension: plain lists of the same {'name','url','dates'}
    shape (from `_build_family_wings`), never containing a redacted person - that
    filtering happens upstream, so unlike an ancestor slot a redacted spouse/child
    has no faint 'Unknown' placeholder to fall back on and is simply absent.
    `siblings` (#115: the lost-aunts/uncles/cousins mitigation, home pedigree
    only - see `_build_family_wings`'s `include_siblings` flag) is the same
    shape again, drawn stacked above the subject in its own column with a
    single grouping bracket; empty/omitted for the person page, which does not
    ask for it.

    Layout, left to right: children (if any) - subject + spouse(s), stacked in one
    column - parents - grandparents. This is the ancestors-only chart's original
    shape with two columns bolted on either side of the subject; when spouses and
    children are both empty the geometry (column x, row y, viewBox) is bit-for-bit
    what the ancestors-only renderer produced before this win, so an existing
    person's pedigree does not visually change. Family lines route couples-first
    (owner request, review 2026-07-17): the subject's and a spouse's lines join at
    that couple's junction before one line splits to that couple's own children -
    the ancestor elbow, mirrored - and children are grouped by which drawn spouse
    is their other parent (each spouse entry carries `id`, each child entry
    `co_parents`, from `_build_family_wings`). Spouses arrive marriage-date-first
    (`_build_family_wings` orders them), and only the FIRST marriage draws the
    solid join-then-split bracket; later marriages render dotted
    (`ped-link-later`) with their children branching at that spouse's own row -
    see the routing loop's comment for the two geometry collisions that rule
    also fixes. Kids with no drawn co-parent hang
    off the subject alone, which is also the privacy-safe fallback for a
    redacted co-parent. The subject sits at the left of
    its own group and each ancestor generation steps rightward - the genealogical
    convention, and the fix for the descendant renderer drawing ancestors
    *downward* (upside-down). Node cards are HTML in <foreignObject> so names wrap
    and links work; a drawn ancestor's un-researched parent shows as a faint
    'Unknown' slot so the bracket reads as a pedigree - children get no such
    placeholder (you cannot enumerate someone's unknown children).

    A 4th column (children) needs more on-screen width than the 620px the
    ancestors-only chart is capped at, so that case gets the `pedigree-family`
    modifier class (a wider max-width in styles.css) plus tighter card/row
    spacing - the size-reduced variant, matching the wireframe's `wb-famchart`
    sizing without pulling in any of its workbench affordances.

    `missing_parent_of` (workbench only, per the plan-17 wireframe's
    `ped-empty[data-wb-open]` cards) maps an empty ancestor slot number to the
    P-id of its known child, so that slot's 'Unknown' becomes a clickable
    'Unknown - add' that opens 'add family' scoped to the right person -
    never shown when `workbench` is false, matching every other workbench-only
    affordance already gated the same way on this page.

    `branch_color` (#115, home pedigree only) tints each drawn ancestor card's
    left edge by paternal/maternal line: an Ahnentafel slot's line is recoverable
    from its own number alone (halve it repeatedly until it lands on 2 or 3 -
    see `_ancestor_branch`), so this costs no extra data, just a slot-number
    check per card - EXCEPT when the slot's own root (2 or 3) was placed by
    elimination among unknown-sex parents rather than matched by a recorded
    `sex:` (`labels[root]['sex_derived']` is False, from `_build_ahnentafel`):
    that root's whole subtree renders with no branch class at all (#152
    review fix, P2) rather than asserting a paternal/maternal fact nobody
    actually recorded. `axis_label` (#115) draws one small caption above the
    ancestor columns ('ancestors of {name} ->'-style orientation text) - omitted
    when there is no ancestor column to sit above (ancestor_generations == 0,
    the redaction-safe hub-only fallback). `home` (#115) is purely a sizing
    flag - the `pedigree-home` CSS modifier class, wider than the person-page
    cap - kept independent of the other new parameters so each stays
    individually testable rather than one flag silently implying another.

    GEOMETRY NOTE (`row_index`, generalized for #115): every rendered slot's row
    is `offset * span + (span - 1) / 2`, where `offset = num - 2**g` is the
    slot's position within its own generation g and `span = 2**(D - g)`. D is
    `max_gen` - the deepest generation actually PLACED in `render` (a real
    ancestor or its still-unresearched 'Unknown' child slot) - NOT the full
    CONFIGURED `ancestor_generations`/`max_ancestor_gen`, which only bounds how
    far the walk below is allowed to look. This is the closed form of "a slot's
    row is the average of its two children's rows, and a leaf slot (g == D)
    occupies row `offset`" - i.e. plain binary-tree centering, unrolled
    algebraically instead of recursed. It reduces to the pre-#115 hardcoded
    constants exactly when a tree happens to reach 2 full generations (subject
    1.5, parents 0.5/2.5, grandparents 0-3 - pinned by a regression test); a
    shallower known tree centres against its OWN shallower depth instead of the
    configured maximum, which is what keeps a sparsely-researched line's chart
    compact even when `ancestor_generations` (`site.home_pedigree_generations`,
    default 5) is configured much deeper - see the `ancestor_band` comment
    below for why keying this off the configured depth instead was a real bug,
    not just a theoretical waste."""
    spouses = spouses or []
    children = children or []
    siblings = siblings or []
    has_children_col = bool(children)
    # A caller passes 0 for the redaction-safe hub-only fallback (#115: no
    # eligible ancestor was found at all) - negative is defensive only, no
    # caller sends it. Never trust a raw negative into the row-centering math
    # below (2**negative is a fraction, not "no ancestors").
    max_ancestor_gen = max(0, ancestor_generations)

    # Group children by which drawn spouse is their other parent (the
    # `co_parents` ids `_build_family_wings` attaches): group 0 is the
    # subject-alone lane (no recorded/drawn co-parent), group i+1 is spouse
    # i's couple. Each group draws its own couple bracket + children trunk,
    # so kids by different spouses hang off different junctions (owner
    # request, review 2026-07-17). Entries without ids (older callers,
    # tests) all land in the subject lane - same as before grouping existed.
    spouse_lane = {normalize_id(str(s['id'])): i + 1
                   for i, s in enumerate(spouses) if s.get('id')}
    child_groups: list[list[int]] = [[] for _ in range(len(spouses) + 1)]
    for idx, ch in enumerate(children):
        co = [normalize_id(str(c)) for c in (ch.get('co_parents') or [])]
        lane = next((spouse_lane[c] for c in co if c in spouse_lane), 0)
        child_groups[lane].append(idx)
    # Lanes that need their own x-stations in the children gap: every couple
    # (bracket, plus a trunk when it has children) and the subject lane only
    # when it actually has children.
    n_lanes = len(spouses) + (1 if child_groups[0] else 0)

    CW = 176
    if has_children_col:
        # The children gap holds two x-stations per lane (couple junction +
        # children trunk), spaced ~8 units apart - one couple keeps the chart
        # compact, extra spouses widen it as they need room.
        CH, ROW, PAD = 48, 60, 8
        COL_GAP = max(24, 8 * (2 * max(n_lanes, 1) + 2))
    else:
        CH, COL_GAP, ROW, PAD = 62, 40, 72, 8

    # Generation index 0 is the subject/spouse column; ancestors step positive
    # (1 = parents, 2 = grandparents); children, when present, take -1 so they
    # sit to the left of the subject as the wireframe lays out.
    min_gen = -1 if has_children_col else 0

    def col_x(gen: int) -> float:
        return PAD + (gen - min_gen) * (CW + COL_GAP)

    # Draw the subject always; an ancestor slot only when its child is a drawn person -
    # real ancestors as name cards, a known person's missing parent as a faint 'Unknown'.
    # Slots visit in increasing numeric order (2, 3, 4, 5, ... up to the deepest
    # generation asked for) - slot//2 (a slot's parent) is always a smaller
    # number than the slot itself, so by the time any slot is checked its
    # parent's render state is already decided. This is the #115
    # generalization of the old literal `for slot in (2, 3, 4, 5, 6, 7)` (D=2)
    # to any configured depth D, with no change to the walk order or the rule
    # itself - see `_build_ahnentafel`, which already supported arbitrary
    # depth (the fan chart has called it with max_gen=3 since win 1).
    render: dict[int, tuple] = {1: ('person', labels.get(1) or {'name': ''})}
    for slot in range(2, 1 << (max_ancestor_gen + 1)):
        if render.get(slot // 2, ('', None))[0] != 'person':
            continue
        lab = labels.get(slot)
        render[slot] = ('person', lab) if (lab and lab.get('name')) else ('empty', None)
    # Deepest generation actually PLACED in `render` - the depth research (or
    # its still-unresearched 'Unknown' placeholders) actually reached, which
    # may be far shallower than `max_ancestor_gen` (the full CONFIGURED depth
    # a caller asked for) when every ancestor beyond some point is
    # unresearched (an 'Unknown' slot's own kind is 'empty', so the loop above
    # never places ITS children). Used below for BOTH the overall width (as
    # before #115) and - critically - as the basis for `row_index`'s row
    # spacing and the reserved `ancestor_band` height: a chart must centre and
    # reserve space against the depth it actually drew, not the depth a
    # caller was willing to look. Getting this wrong (keying height off
    # `max_ancestor_gen` instead) is exactly the home-pedigree geometry bug
    # fixed here - see the GEOMETRY NOTE and `ancestor_band` comments.
    max_gen = max((k.bit_length() - 1 for k in render), default=0)

    def row_index(num: int) -> float:
        """Row position (in ROW units) for an Ahnentafel slot, centred against
        `max_gen` - the depth actually reached by `render` above, NOT the full
        configured `ancestor_generations`/`max_ancestor_gen` - generations of
        leaf rows; see the GEOMETRY NOTE above for the derivation. Pure
        function of `num` and the closed-over `max_gen`, so spouse/children/
        sibling rows can be placed relative to the same scale (`subject_row =
        row_index(1)` below, not a separate constant - the two must never
        drift apart, or the subject card drawn by the ancestor loop and the
        subject row every other column is measured against would land on two
        different y's). `render`/`max_gen` are computed just above, before
        this closure's first call, precisely so this centres on data that was
        actually drawn rather than on how deep the caller was willing to
        look."""
        g = num.bit_length() - 1
        offset = num - (1 << g)
        span = 1 << (max_gen - g)
        return offset * span + (span - 1) / 2

    subject_row = row_index(1)
    spouse_rows = [subject_row + 1 + i for i in range(len(spouses))]     # stack below the subject
    # Siblings (#115 lost-relatives mitigation, home pedigree hub only - see
    # `_build_family_wings`'s `include_siblings` flag) stack ABOVE the
    # subject's own row, in the SAME column (col_x(0)) - the mirror of how
    # spouses stack below - so they read as "beside the subject" rather than
    # as another ancestor generation. A single bracket groups them (drawn
    # below, alongside the spouse/children links) instead of each getting its
    # own connector into an ancestor slot the sibling does not own on this
    # chart (their shared parents are already drawn once, for the subject).
    sib_rows = [subject_row - 1 - i for i in range(len(siblings))]
    # Children stack top-to-bottom in lane order - subject-only children
    # first (nearest the subject's row), then each couple's in spouse order -
    # so a lane's kids sit near its junction and trunks stay short. The
    # column is centred on the whole family band (subject through last
    # spouse), not the subject alone, so a two-spouse chart reads balanced.
    ordered_children = [i for lane in child_groups for i in lane]
    n_children = len(children)
    family_mid = (subject_row + spouse_rows[-1]) / 2 if spouse_rows else subject_row
    # #120 (reopened twice after the original stagger fix): whenever 2+
    # co-parent lanes share the children gap - a subject-alone lane plus a
    # drawn spouse, or 2+ drawn spouses - the combined children band's own
    # row grid can land EXACTLY on a row a DIFFERENT lane already uses for
    # something else: the subject's own row, a spouse's own row, or the
    # first marriage's branch-to-children point (always subject_row + 0.5,
    # since spouse_rows[0] is always subject_row + 1 - see `branch_y`
    # below). That happens because `family_mid` sits at subject_row +
    # len(spouses)/2, which is always a whole or half ROW step - the SAME
    # grid every one of those reserved rows lives on. A foreign child
    # landing on a reserved row makes that row's OTHER line (a couple
    # bracket's final leg into the spouse's card, or a branch stub) run
    # collinear with that child's own spoke line into the children column -
    # three real, reported overlaps, found one at a time on real charts
    # (subject-alone-vs-spouse-0 bracket departure, fixed separately above
    # via `stagger`; the first marriage's branch point vs a subject-alone
    # child; a spouse's own card row vs a subject-alone child).
    #
    # Nudging the WHOLE children band off that grid by a small constant -
    # not tied to the spouse count's parity, so it can never re-align by
    # coincidence the way a per-case patch would - removes every row a
    # FOREIGN lane could ever land on. A lane's OWN branch/trunk vertical
    # already spans from its branch point to its own children's extremes
    # regardless of whether any one child sits exactly on that point (see
    # `span` below), so shifting a same-lane child a few px off its own
    # lane's branch row is still correct, ordinary geometry - not a
    # collision fix undone. A single lane (no drawn spouse, or one spouse
    # with no subject-alone kids) has no other lane to land on top of, so
    # the nudge only applies once there are 2+ lanes drawn - every chart
    # this bug never touched keeps its exact pre-existing row numbers.
    if n_lanes >= 2:
        family_mid += 0.25
    children_rows = [family_mid + (i - (n_children - 1) / 2) for i in range(n_children)]
    # Row of each child by its ORIGINAL index (cards and ticks look rows up here).
    child_row_of = {orig: children_rows[pos] for pos, orig in enumerate(ordered_children)}

    # The ancestor band is always the full grandparent grid for the depth
    # ACTUALLY reached (`max_gen`, computed above): slot 1 (the subject) is
    # unconditionally `('person', ...)`, so the render loop unconditionally
    # places slots 2 and 3 too - as a known name or a faint 'Unknown' - the
    # moment the subject draws (i.e. always), and the walk keeps placing real-
    # or-'Unknown' slots one generation deeper for as long as the previous
    # generation actually resolved to a real person. There is no input for
    # which the deepest PLACED slots (generation `max_gen`) are empty of a
    # card, so a band shallower than `max_gen`'s own leaf-row range always
    # undercounts the cards about to draw there.
    #
    # This used to be sized off `len(labels)` (the count of KNOWN ancestors)
    # on the theory that a chart with no ancestors "has no reason to reserve
    # that band" - but an unresolved parent never shows up in `labels` at all
    # (it lives only in `missing_parent_of`), so a person with zero known
    # parents still got two 'Unknown' cards at their normal offset rows while
    # the old check collapsed the band down to the subject's own row -
    # clipping both cards outside the computed viewBox (#119). Spouse/
    # children rows then extend the band only when they reach beyond it
    # (extra spouses stacking past row 3, a wide brood of children reaching
    # above row 0).
    #
    # Generalized for #115 as [0, 2**D - 1] with D = `max_ancestor_gen` (the
    # full CONFIGURED depth, `ancestor_generations` /
    # `site.home_pedigree_generations`, default 5) - then corrected here to
    # key D off `max_gen` (the depth ACTUALLY reached) instead. Reserving the
    # full configured depth's leaf-row band regardless of how far research
    # actually got was harmless at the pre-#115 hardcoded D=2 (worst case 4
    # reserved rows, ~294px), but a real bug once D defaults to 5 for the home
    # pedigree: an archive that is not researched 5 generations deep on every
    # line got a chart reserved for up to 32 leaf rows it never used, spacing
    # the few real cards hundreds of px apart and - via the JS pan/zoom
    # viewport's fit-to-height calculation - shrinking the WHOLE chart's
    # initial render scale (name text included) well below legible size. The
    # band is now [0.0, 3.0] when a tree happens to reach 2 full generations
    # (the pre-#115 shape, unchanged), [0.0, 0.0] (just the subject's own row)
    # when `max_gen` is 0 - the redaction-safe hub-only fallback with no
    # ancestor columns at all - and scales with whatever depth the walk
    # actually placed cards at in between, no matter how deep it was
    # CONFIGURED to look.
    ancestor_band = [0.0, float((1 << max_gen) - 1)]
    all_rows = ancestor_band + spouse_rows + children_rows + sib_rows
    min_row, max_row = min(all_rows), max(all_rows)
    # A small extra top margin reserves room for the axis-label caption
    # (#115) - only when there is an ancestor column for it to sit above; the
    # hub-only fallback (max_gen == 0, no ancestor columns drawn) never asks
    # for one, but a caller-supplied label with nothing to caption is still
    # dropped defensively rather than floating unlabeled space at the top.
    axis_pad = 22.0 if (axis_label and max_gen >= 1) else 0.0
    base = PAD + axis_pad + CH / 2 - min_row * ROW

    def y_center(row: float) -> float:
        return base + row * ROW

    W = 2 * PAD + (max_gen - min_gen + 1) * CW + (max_gen - min_gen) * COL_GAP
    H = 2 * PAD + axis_pad + CH + (max_row - min_row) * ROW

    def yr(edtf) -> str:
        m = re.search(r'\d{4}', str(edtf)) if edtf else None
        return m.group(0) if m else ''

    def card(x: float, yc: float, cls_extra: str, lab: dict | None, slot: int | None = None,
             branch: int | None = None) -> str:
        div_attrs = ''
        if lab is None:
            child_pid = missing_parent_of.get(slot) if (workbench and missing_parent_of and slot is not None) else None
            cls, inner = 'ped-node ped-empty', '<span class="ped-name">Unknown</span>'
            if child_pid:
                # Same 'click the empty ancestor slot' affordance the plan-17
                # wireframe mocked (person.html's ped-empty[data-wb-open]
                # cards): opens 'add family' scoped to the known child one
                # generation closer, relation parent - existing modal, no new
                # capability, matching the "+ add" links elsewhere on this page.
                # data-wb-fill presets the VISIBLE relation select to parent -
                # without it, collect() lets the select's non-blank default
                # ('sibling') silently override the fixed arg, so clicking an
                # ancestor slot recorded a sibling (owner-reported bug,
                # live review 2026-07-16).
                args = html.escape(json.dumps(
                    {'person_id': fmt_id_display(child_pid), 'relation_type': 'parent'}), quote=True)
                inner = '<span class="ped-name">Unknown &mdash; add</span>'
                div_attrs = (f' data-wb-open="tpl-add-family" data-wb-args=\'{args}\' '
                            'data-wb-fill="relation_type=parent" '
                            'title="Create a stub for this ancestor" role="button" tabindex="0"')
        else:
            cls = 'ped-node' + cls_extra
            if branch:
                # #115: paternal/maternal tint (only for a real ancestor card -
                # the subject's own '.ped-self' styling already carries a fixed
                # accent, and an 'Unknown' placeholder has no line to name).
                cls += f' ped-branch-{branch}'
            if lab.get('hypothesis'):
                # Workbench-only (the builder only sets the flag there): an
                # unsourced frontmatter tie fills the slot but is visibly not
                # a claim-backed edge - dashed like the strip's (hypothesis)
                # tag, with the lifecycle spelled out on hover.
                cls += ' ped-hypothesis'
                div_attrs = ' title="unsourced hypothesis - source it in review"'
            name = html.escape(lab.get('name') or '')
            url = lab.get('url')
            name_el = (f'<a class="ped-name" href="{html.escape(url, quote=True)}">{name}</a>'
                       if url else f'<span class="ped-name">{name}</span>')
            d = lab.get('dates') or {}
            b, dd = yr(d.get('birth')), yr(d.get('death'))
            span = f'{b}–{dd}' if (b and dd) else (f'b. {b}' if b else (f'd. {dd}' if dd else ''))
            inner = name_el + (f'<span class="ped-dates">{span}</span>' if span else '')
        return (f'<foreignObject x="{x:.0f}" y="{yc - CH / 2:.0f}" width="{CW}" height="{CH}">'
                f'<div xmlns="http://www.w3.org/1999/xhtml" class="{cls}"{div_attrs}>{inner}</div>'
                f'</foreignObject>')

    links: list[str] = []
    cards: list[str] = []
    for slot, (kind, lab) in render.items():
        x = col_x(slot.bit_length() - 1)
        yc = y_center(row_index(slot))
        for pslot in (2 * slot, 2 * slot + 1):       # elbow to each drawn ancestor's parent
            if pslot in render:
                x2, y2 = col_x(pslot.bit_length() - 1), y_center(row_index(pslot))
                midx = (x + CW + x2) / 2
                links.append(f'<path class="ped-link" d="M{x + CW:.0f},{yc:.0f} '
                             f'H{midx:.0f} V{y2:.0f} H{x2:.0f}"/>')
        branch = None
        if branch_color and slot != 1 and kind == 'person':
            # #152 review fix (P2): a slot's branch color is only as good as
            # the evidence behind whichever slot (2 or 3) it ultimately
            # reduces to (`_branch_root`) - when THAT root was placed by
            # elimination among unknown-sex parents rather than matched by a
            # recorded `sex:` (`sex_derived` is False, from
            # `_build_ahnentafel`), the slot-2-vs-3 split itself is
            # order-dependent, not evidence, so tinting it paternal/maternal
            # would assert a fact nobody actually recorded - and could even
            # swap which color an unknown-sex parent's line gets between
            # builds. Default True (undecided root, e.g. no ancestors drawn
            # at all) preserves the pre-fix look for every ordinary chart.
            root_label = labels.get(_branch_root(slot)) or {}
            if root_label.get('sex_derived', True):
                branch = _ancestor_branch(slot)
        cards.append(card(x, yc, ' ped-self' if slot == 1 else '', None if kind == 'empty' else lab,
                          slot, branch))

    subj_x = col_x(0)
    subj_y = y_center(subject_row)
    for i, lab in enumerate(spouses):
        cards.append(card(subj_x, y_center(spouse_rows[i]), '', lab))
    for i, lab in enumerate(children):
        cards.append(card(col_x(-1), y_center(child_row_of[i]), '', lab))
    for i, lab in enumerate(siblings):
        cards.append(card(subj_x, y_center(sib_rows[i]), ' ped-sibling', lab))
    if siblings:
        # One grouping bracket for the whole sibling stack - a straight line
        # from the subject's own row up through the topmost sibling, drawn
        # BEHIND the cards (links are emitted before cards below), the same
        # "line passes through the column, cards sit on top" idiom the
        # spouses-with-no-children bracket already uses just below.
        sib_ys = [subj_y] + [y_center(r) for r in sib_rows]
        if len(set(sib_ys)) > 1:
            links.append(f'<path class="ped-link ped-link-sibling" d="M{subj_x:.0f},{min(sib_ys):.0f} '
                         f'V{max(sib_ys):.0f}"/>')

    if children:
        # Couples-first routing (owner request, review 2026-07-17): the
        # subject's and each spouse's lines COME TOGETHER at that couple's
        # junction before splitting to that couple's own children - the
        # mirror image of the ancestor elbows on the right. Each lane gets
        # two x-stations in the children gap: an outer one (nearer the
        # family column) where the couple bracket joins, and an inner one
        # (nearer the children) where the trunk splits to the kids. Lanes
        # step leftward in order, so a second marriage's lines never overlap
        # the first's - horizontals may cross other lanes' verticals at
        # right angles (readable as crossing wires), but no two lanes share
        # a collinear segment.
        gap_right = subj_x
        step = (subj_x - (col_x(-1) + CW)) / (2 * max(n_lanes, 1) + 2)

        def lane_stations(k: int) -> tuple[float, float]:
            """(junction_x, trunk_x) for draw-lane k (0 nearest the family)."""
            return gap_right - (2 * k + 1) * step, gap_right - (2 * k + 2) * step

        draw_lane = 0
        # Subject-only children first: their line leaves the subject's own
        # card - there is no couple to join - then splits at its trunk.
        if child_groups[0]:
            _, trunk_x = lane_stations(draw_lane)
            draw_lane += 1
            child_ys = [y_center(child_row_of[i]) for i in child_groups[0]]
            span = child_ys + [subj_y]
            links.append(f'<path class="ped-link" d="M{subj_x:.0f},{subj_y:.0f} H{trunk_x:.0f}"/>')
            if len(set(span)) > 1:
                links.append(f'<path class="ped-link" d="M{trunk_x:.0f},{min(span):.0f} '
                             f'V{max(span):.0f}"/>')
            for cy in child_ys:
                links.append(f'<path class="ped-link" d="M{col_x(-1) + CW:.0f},{cy:.0f} H{trunk_x:.0f}"/>')
        for i in range(len(spouses)):
            junction_x, trunk_x = lane_stations(draw_lane)
            draw_lane += 1
            spouse_y = y_center(spouse_rows[i])
            # First marriage vs later marriages (owner decision, review
            # 2026-07-17): the FIRST couple keeps the solid join-then-split
            # bracket; every LATER couple renders dotted (`ped-link-later`)
            # and its children branch at that spouse's OWN row. Two reasons
            # beyond taste: (a) the midpoint of the subject and spouse k
            # always lands exactly on spouse k-1's row (spouse rows are one
            # ROW apart), so a midpoint split for a later couple looked like
            # it emanated from the previous spouse; (b) a later bracket
            # retracing the first one's horizontal out of the subject card
            # read as one line that forks, so later brackets attach a few px
            # lower (clamped inside the card edge). That same stagger used to
            # be indexed on `i` alone, which never accounts for a
            # subject-alone lane (drawn just above, when `child_groups[0]` is
            # non-empty) already sitting AT the subject's raw row: spouse 0's
            # stagger evaluates to zero, so its bracket left the subject card
            # on the exact row the subject-alone lane's own horizontal
            # segment already occupies - a true collinear overlap between two
            # lanes (#120). Bumping every spouse's stagger index by one
            # notch whenever a subject-alone lane was drawn keeps spouse 0
            # off that row without moving anything when there is no
            # subject-alone lane to collide with.
            later = i > 0
            cls = 'ped-link ped-link-later' if later else 'ped-link'
            stagger = i + (1 if child_groups[0] else 0)
            attach_y = min(subj_y + 6 * stagger, subj_y + CH / 2 - 4)
            # The couple bracket: subject and spouse join at the junction...
            links.append(f'<path class="{cls}" d="M{subj_x:.0f},{attach_y:.0f} '
                         f'H{junction_x:.0f} V{spouse_y:.0f} H{subj_x:.0f}"/>')
            kids = child_groups[i + 1]
            if not kids:
                continue
            # ...and only then does one line leave toward the children - from
            # the couple's midpoint for the first marriage (the ancestor
            # elbow, mirrored), from the spouse's own row for later ones.
            branch_y = spouse_y if later else (subj_y + spouse_y) / 2
            child_ys = [y_center(child_row_of[c]) for c in kids]
            span = child_ys + [branch_y]
            links.append(f'<path class="{cls}" d="M{junction_x:.0f},{branch_y:.0f} H{trunk_x:.0f}"/>')
            if len(set(span)) > 1:
                links.append(f'<path class="{cls}" d="M{trunk_x:.0f},{min(span):.0f} '
                             f'V{max(span):.0f}"/>')
            for cy in child_ys:
                links.append(f'<path class="{cls}" d="M{col_x(-1) + CW:.0f},{cy:.0f} H{trunk_x:.0f}"/>')
    elif spouses:
        # No children to route to - a direct bracket at the column's left
        # edge is enough to show the subject and spouse(s) as one family
        # unit (there is no children gap to hold a junction station here).
        family_ys = [subj_y] + [y_center(r) for r in spouse_rows]
        if len(set(family_ys)) > 1:
            links.append(f'<path class="ped-link" d="M{subj_x:.0f},{min(family_ys):.0f} '
                         f'V{max(family_ys):.0f}"/>')

    # Orientation cue (#115): a short caption naming the direction the chart
    # reads, set once above the ancestor columns - never drawn when there is
    # no ancestor column (axis_pad is 0 in that case too, so there is no
    # reserved space for it to sit in). Centered (text-anchor: middle,
    # design/styles.css) across the FULL span of ancestor columns actually
    # drawn - col_x(1)'s left edge to col_x(max_gen)'s right edge - rather
    # than left-anchored at col_x(1) alone (#152 review fix): at the
    # person-page chart's fixed 2 generations that used to read as roughly
    # centered by coincidence, but a deep home-page chart (5+ generations by
    # default) left the caption labeling only the nearest column instead of
    # the whole ancestor block beneath it.
    extra: list[str] = []
    if axis_label and axis_pad:
        axis_center_x = (col_x(1) + col_x(max_gen) + CW) / 2
        extra.append(f'<text class="ped-axis-label" x="{axis_center_x:.0f}" '
                     f'y="{PAD + axis_pad - 6:.0f}">{html.escape(axis_label)}</text>')

    svg_cls = 'pedigree pedigree-family' if has_children_col else 'pedigree'
    if home:
        # The home pedigree (#115) is deliberately wider than the person-page
        # cap (design/styles.css) - the deeper default depth needs more room
        # than a compact per-person chart, and it is always wrapped by the
        # pan/zoom viewport (fha-tree.js's `wrapStatic`) rather than read at
        # full natural size.
        svg_cls += ' pedigree-home'
    label = 'Family chart' if (spouses or children or siblings) else 'Ancestor pedigree'
    return (f'<svg class="{svg_cls}" viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid meet" '
            f'role="img" aria-label="{label}">' + ''.join(links) + ''.join(cards) + ''.join(extra) + '</svg>')


# ── Paths / hrefs ───────────────────────────────────────────────────────────

def _rel_href(target: Path, page_dir: Path) -> str:
    """Relative href (forward-slash) from a page's directory to a target file.

    Used in `--linked` mode to point at real archive assets, and for media
    derivatives in `--standalone`. `os.path.relpath` raises ValueError when the
    two paths are on different Windows drives (an external asset root on D:\\,
    site on C:\\) - fall back to a `file://` absolute URI so the link still
    resolves rather than emitting a broken relative path.
    """
    try:
        rel = os.path.relpath(target, page_dir)
        return Path(rel).as_posix()
    except ValueError:
        return target.resolve().as_uri()


def _role_note(role: str | None, copy: str | None, date_edtf: str | None = None) -> str | None:
    """Plain-language annotation for one source-page file entry: its `files:`
    entry's optional per-file `date:` (SPEC §14, #123) first, then its
    `role:`, then - when set - its `copy:` letter (SPEC §14's `copy: b`/`c`/`d`
    same-day/same-item variant marker - #123 also fixed the indexer landing
    that value as NULL instead of reading it). Renders '26 February 1916 ·
    role: entry · copy: b', or just 'role: entry' when there is
    neither, so a source with a single, undated file per role keeps today's
    shorter label and only a run of look-alike variants (front/back copy
    A, front/back copy B, several same-role entries of one household ledger
    or pages of a multi-sitting letter, …) gains the extra clauses that tell
    its files apart.

    `date_edtf` is rendered through `_lib.humanize_edtf` - the shared EDTF ->
    plain-English helper (decades, month/day, and `~`/`?` hedges rendered as
    the two different things they record - "about" for approximate, "
    (unconfirmed)" for uncertain - all handled there already) - rather than a
    second hand-rolled version of the same formatting living in this module.
    Moved into `_lib.py` (#123 follow-up, Codex review on PR #149) so this
    module no longer has to import the whole `photoindex` tool just to reach
    one date formatter."""
    parts: list[str] = []
    if date_edtf:
        parts.append(_humanize_edtf(date_edtf))
    if role:
        parts.append(f'role: {role}')
    if copy:
        parts.append(f'copy: {copy}')
    return ' · '.join(parts) if parts else None


def _with_role_note(message: str, role_note: str | None) -> str:
    """Compose a fixed availability/status message with the file's own
    `_role_note` output (date/role/copy), so a degraded display path (an
    asset that cannot be resolved or is missing on disk, a standalone build
    without Pillow, a failed image-derivative creation, a file omitted for
    naming a living person, …) still surfaces the per-file facts the index
    already has instead of discarding them outright. Only the FILE's
    presentability is degraded on these paths - not the indexed facts about
    it - so the message adds to `role_note` rather than replacing it (#123
    follow-up, Codex review on PR #149: `_file_entry`/`_standalone_image_entry`
    used to drop date/role/copy entirely on every one of these branches; a
    later proactive audit found the same bug shape a third time in the
    living-tagged-photo gate inside `_source_file_entries`). `role_note`
    first, message last, matching the join order the 'original kept in the
    archive' branch already used before this fix generalized the pattern to
    every fallback branch across the module."""
    return f'{role_note} · {message}' if role_note else message


def _page_filename(record_id: str) -> str:
    """Normalized id → page filename, e.g. 'P-de957bcda1' → 'p-de957bcda1.html'."""
    return f'{normalize_id(record_id)}.html'


def _json_for_script(obj) -> str:
    """Serialize `obj` for safe embedding inside an inline <script> element.

    A bare `</script>` (or a `<!--`) inside JSON would close the script tag and
    let the rest be parsed as HTML - an injection vector. Escaping `<`, `>`, and
    `&` as JSON unicode escapes keeps the value valid JSON while making a
    `</script>` sequence impossible. The result is read back via
    `JSON.parse(scriptEl.textContent)`, never fetched (file:// has no network)."""
    return (json.dumps(obj, ensure_ascii=False)
            .replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026'))


# ── Builder ─────────────────────────────────────────────────────────────────

class _SiteBuilder:
    """Holds the shared state for one site build and renders every page.

    Constructed once per `run_site`. `prepare()` loads the person/source
    metadata and decides - once, up front - which person and source pages will
    exist under the active mode. Every cross-link and every token-swap then
    consults that single decision (`self.person_pages` / `self.source_pages`),
    so the site can never link to a page it didn't generate (the standalone
    redaction-symmetry rule). `messages` accumulates plain-language warnings the
    CLI prints to stderr; a non-empty list means the build finished with
    warnings (exit 1), not failure.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        archive_root: Path,
        fha_config: dict,
        out_dir: Path,
        *,
        linked: bool,
        workbench: bool = False,
        workbench_context: dict | None = None,
    ) -> None:
        self.conn = conn
        self.archive_root = archive_root
        self.fha_config = fha_config
        self.out_dir = out_dir
        self.linked = linked          # False = standalone (default, redacted)
        # Workbench mode is serve-only (never a CLI surface): it turns on the
        # editing chrome in the templates (`{% if workbench %}`) and rewrites
        # asset hrefs to serve's /root/<alias>/ URLs so photos/documents that
        # live outside the snapshot resolve over HTTP instead of escaping it
        # with ../../ relative links. It REQUIRES linked mode (unredacted) - the
        # combination workbench+standalone is refused in run_site. Nothing here
        # ever leaks into a standalone build: every branch is guarded on
        # self.workbench, which is False for both `fha site` modes.
        self.workbench = workbench
        self.workbench_context = workbench_context or {}
        self.messages: list[str] = []

        self.persons_dir = out_dir / 'persons'
        self.sources_dir = out_dir / 'sources'
        self.places_dir = out_dir / 'places'
        self.media_dir = out_dir / 'media'
        self.data_dir = out_dir / 'data'       # neutral tree JSON artifacts (M8.5)
        self.vendor_dir = out_dir / 'vendor'    # vendored tree renderer + adapter (M8.5)
        self.assets_dir = out_dir / 'assets'    # design system: stylesheet, override, fonts

        self.person_meta: dict[str, sqlite3.Row] = {}
        self.source_meta: dict[str, sqlite3.Row] = {}
        self.place_meta: dict[str, sqlite3.Row] = {}
        self.place_names: dict[str, str] = {}   # id → display name (token rendering)
        # pid -> its open '## Q:' blocks (issue #117), built once in prepare()
        # by _load_open_questions - only when self.linked (see
        # build_person_page for why this never reaches a public/standalone
        # build).
        self.person_questions: dict[str, list[dict]] = {}
        # A research file's OWN pid -> whether ITS person carries `restricted:
        # by-request` (memoized so a person with several open questions in
        # their research file has their profile read once, not once per
        # question). Filled lazily by `_person_is_by_request`, consulted by
        # `_question_origin_is_by_request` - see `_load_open_questions` for
        # why this check cannot reuse `self.restricted_persons`.
        self._by_request_origin_cache: dict[str, bool] = {}
        self.alias_map: dict[str, str] = {}     # lowercased name/stem → canonical id
        self.person_pages: set[str] = set()   # normalized pids that get a page
        self.source_pages: set[str] = set()   # normalized sids that get a page
        self.place_pages: set[str] = set()    # normalized lids that get a page
        # The `restricted` marker at the claim/person/name level, read from the
        # record files once in prepare() (the index carries none of these). A
        # restricted source caught only by a free-text type also lands here.
        self.restricted_persons: set[str] = set()       # pids withheld from public output
        self.restricted_sources: set[str] = set()        # sids the index 0/1 missed
        # sids whose only reason to be withheld is that they name a person-level
        # restricted person (`restricted: by-request` on a deceased individual).
        # Kept alongside `restricted_sources` so `_source_hard_restricted` can
        # treat these as intentionally private too - otherwise the deceased
        # person's facts would publish through a redacted citation.
        self.restricted_person_sources: set[str] = set()
        self.restricted_claims: set[str] = set()         # claim ids withheld
        # Whose OWN vital each vital claim supplies (`_lib.vital_subjects`),
        # memoized per claim: the summary block and every chart node ask it,
        # so a person named on twenty records would otherwise re-read the same
        # claim_persons rows twenty times. None means the claim never said.
        self._vital_subjects: dict[str, list[str] | None] = {}
        self.restricted_names: dict[str, set[str]] = {}   # pid → lowercased restricted variant values
        # Opened once in prepare() when the photo index is fresh, reused across
        # every person page, closed by run_site - so the photos-root freshness
        # walk happens once per build, not once per curated person.
        self.photos_conn: sqlite3.Connection | None = None
        # Why that catalog is not usable, if it is not: one of _PHOTO_CATALOG_
        # TROUBLE's keys, or 'fresh' once it has opened. Kept because the
        # living-person photo gate fails OPEN by design - the site is meant to
        # build without a catalog - so the one thing the build owes the
        # researcher is to say which check did not run, and why.
        self.photos_status: str = 'absent'
        self._living_photo_warning_sent = False
        # discoveries.md is read for both the discoveries page and the home
        # teaser; memoize so the file is parsed once per build.
        self._discoveries: tuple[str, list[str]] | None = None
        # A person's profile photo is resolved once (front-matter read + photo
        # lookup + derivative) and reused across their page and every tree node
        # they appear in. Value: the publishable image file, or None.
        self._profile_photo_cache: dict[str, Path | None] = {}
        # `_provisional_vital` is asked up to four times per person page
        # (birth/death x date/place - PROVISIONAL_VITAL_FIELDS) and each ask
        # used to independently re-read and re-parse the same record file
        # from disk (audit finding: the same duplicate-expensive-call shape
        # already fixed once in report.py's places.run_candidates() case).
        # One parsed record per person, memoized here, serves all four asks.
        # None means "read failed or the file is undecodable" (cached too,
        # so a broken file is not retried four times either) - distinct from
        # "not yet asked" (pid absent from the dict).
        self._provisional_record_cache: dict[str, dict | None] = {}
        # One person's `relationships` rows for a given direction ('parent' or
        # 'child'), memoized across every descendant/ancestor tree BFS in this
        # build (#152 review fix, P2, finding 1 - see `_DESCENDANT_TREE_MAX_
        # HOPS`'s comment). Curated people's descendant trees overlap heavily
        # along a shared lineage - a person's descendants include their
        # children's descendants, and so on - so without this cache a lineage
        # of N curated people re-issues roughly the same SELECT up to N times,
        # walking the same shrinking tail each page below re-walks in full.
        # Keyed (person_id, rel); value is the raw `sqlite3.Row` list.
        self._tree_edges_cache: dict[tuple[str, str], list] = {}
        # Per-person-page footnote registry: cited sources become numbered
        # footnotes (superscripts inline, names listed at the bottom) instead of
        # raw [S-id] chips. None outside a person page (e.g. place pages), where
        # a source instead renders as a plain named link. Reset per person page.
        self._footnotes: dict[str, int] | None = None
        self._footnote_seq: list[str] = []

        if jinja2 is not None:
            self.env = jinja2.Environment(
                loader=jinja2.FileSystemLoader(str(Path(__file__).parent / 'templates')),
                autoescape=jinja2.select_autoescape(['html']),
            )
        else:  # pragma: no cover - guarded earlier in run_site
            self.env = None

    # - preparation -

    def prepare(self) -> None:
        """Load metadata and decide which pages exist under the active mode."""
        for row in self.conn.execute(
            'SELECT id, name, surname, sex, living, tier, status, merged_into, path FROM persons'
        ):
            self.person_meta[row['id']] = row
        for row in self.conn.execute(
            'SELECT id, title, source_type, date_edtf, date_min, repository, source_class, '
            'restricted, publication_ok, status, path FROM sources'
        ):
            self.source_meta[row['id']] = row
        for row in self.conn.execute(
            'SELECT id, name, hierarchy, within, lat, lon, notes FROM places'
        ):
            self.place_meta[row['id']] = row
            self.place_names[row['id']] = row['name'] or ''

        # Read the claim/person/name-level `restricted` markers from the record
        # files (the index carries none of them) before deciding which pages
        # exist, so the predicates below see a restricted person/source. Skipped
        # in --linked mode (the dev preview applies no redaction at all).
        if not self.linked:
            self._load_restriction_markers()

        # Alias resolve map (clash-aware): lets a prose `[[Ken Smith]]` / `[[stem]]`
        # name-link resolve to its canonical ID so a living person referenced only
        # by name is still redacted - the privacy hole the display-text form opens.
        self._alias_table_ok: bool = False
        idx: dict[str, set[str]] = {}
        try:
            for alias, cid in self.conn.execute('SELECT alias, canonical_id FROM aliases'):
                idx.setdefault(alias, set()).add(cid)
            self._alias_table_ok = True
        except sqlite3.OperationalError:
            pass   # pre-alias index: no name-link resolution, ID tokens still work
        self.alias_map: dict[str, str] = {
            a: next(iter(ids)) for a, ids in idx.items() if len(ids) == 1
        }
        # The full name→candidates multimap (before the single-id filter above).
        # A name that clashes across ≥2 records is dropped from alias_map, so a
        # `[[Ambiguous Name]]` link fails to resolve - and if any candidate is a
        # living/restricted person, rendering the literal name would leak it.
        # Kept so render_token can fail closed on that case (SPEC §21).
        self._alias_candidates: dict[str, set[str]] = idx
        # A restricted name variant (deadname) is stored mangled in the index
        # alias table (as the stringified mapping), so `[[prior name]]` would not
        # resolve there. Register its value here so the link still resolves to
        # the person internally (SPEC §18) - render_token then redacts the
        # display. Only add an unambiguous value (don't override an existing
        # alias / introduce a silent clash).
        for rid, values in self.restricted_names.items():
            for value in values:
                if value and value not in self.alias_map:
                    self.alias_map[value] = rid

        # Build the set of source ids that name a living person - checked via both the
        # explicit source_people table (frontmatter `people:`) and via claim_persons
        # (claims attached to the source that name a living participant).
        source_living: set[str] = set()
        if not self.linked:
            for row in self.conn.execute(
                "SELECT sp.source_id FROM source_people sp JOIN persons p ON sp.person_id = p.id "
                "WHERE p.living IN ('true','unknown')"
            ):
                source_living.add(row['source_id'])
            for row in self.conn.execute(
                "SELECT DISTINCT c.source_id FROM claims c "
                "JOIN claim_persons cp ON c.id = cp.claim_id "
                "JOIN persons p ON cp.person_id = p.id "
                "WHERE p.living IN ('true','unknown') AND c.source_id IS NOT NULL"
            ):
                source_living.add(row['source_id'])
            # Also exclude sources naming a person-level restricted person
            # (deceased but carrying `restricted: by-request`). The person's
            # page is suppressed by _person_is_redacted; the source page must
            # follow suit so the facts don't leak through the source view.
            if self.restricted_persons:
                placeholders = ','.join('?' * len(self.restricted_persons))
                rp = list(self.restricted_persons)
                for row in self.conn.execute(
                    f"SELECT sp.source_id FROM source_people sp WHERE sp.person_id IN ({placeholders})",
                    rp,
                ):
                    source_living.add(row['source_id'])
                    self.restricted_person_sources.add(row['source_id'])
                for row in self.conn.execute(
                    f"SELECT DISTINCT c.source_id FROM claims c "
                    f"JOIN claim_persons cp ON c.id = cp.claim_id "
                    f"WHERE cp.person_id IN ({placeholders}) AND c.source_id IS NOT NULL",
                    rp,
                ):
                    source_living.add(row['source_id'])
                    self.restricted_person_sources.add(row['source_id'])

        for sid, row in self.source_meta.items():
            if self.linked or not self._source_is_redacted(row):
                # Standalone: also exclude sources whose people list includes a living person.
                if not self.linked and sid in source_living:
                    continue
                self.source_pages.add(sid)
        # Places are never themselves redacted (a place is not a living person);
        # every registry place gets a page, and the person links inside it follow
        # the same redaction rule as everywhere else.
        self.place_pages.update(self.place_meta)
        for pid, row in self.person_meta.items():
            if (row['tier'] or '') != 'curated' and not self.workbench:
                continue          # stubs/connections get no standalone page (TOOLING §12);
                                  # the WORKBENCH gives every recorded person a page so a
                                  # stub is viewable and editable in the editing interface
                                  # (owner decision, live review 2026-07-16) - published
                                  # and plain --linked output stay curated-only.
            if self.linked or not self._person_is_redacted(row):
                self.person_pages.add(pid)

        # linked-or-workbench (build_person_page): parsing/indexing every
        # open question is skipped entirely on a standalone (public) build,
        # where the section never renders. A `## Q:` block still carries no
        # `restricted:` field of its own (issue #117 follow-up, unresolved as
        # of this fix), but `--linked` is this codebase's own established
        # boundary for "real content, not yet safe to publish" - the same
        # trust boundary `drop_private=not self.linked` and
        # `_person_is_redacted` already rely on - so a `## Q:` block gets the
        # same treatment rather than a narrower, workbench-only carve-out.
        if self.linked:
            self._load_open_questions()

        self._open_photos()

    def _load_restriction_markers(self) -> None:
        """Read the claim/person/name-level `restricted` markers from disk.

        The index records `restricted` only on sources, and only as 0/1, so the
        person-level marker, the per-claim marker, the per-name-variant marker,
        and a free-text source type (`restricted: by-request`) are all invisible
        to it. This one pass over the person and source records fills the four
        sets the redaction predicates consult.

        A RECORD THAT CANNOT BE READ IS TREATED AS RESTRICTED. This is an
        exclusion set built by reading files, which means the failure mode of a
        read is not "less content" but "no marker" - and no marker reads
        exactly like a person who never asked to be left out. "Cannot be read"
        covers both ways it happens, because both end in the same empty
        frontmatter: an exception, and the `parse_errors` list `read_record`
        returns INSTEAD of raising for the ordinary cases (a file that is gone,
        a permission error, malformed YAML - `_lib.read_record` hands those
        back as an E010 entry with `meta` empty, so an `except` arm alone would
        have caught almost none of them). It used to skip:
        a person carrying `living: false` and `restricted: by-request` whose
        file would not open got a page on the PUBLIC snapshot, with their name
        on it, while the only sign was a warning about missing prose. That is
        fail-open on the one axis this codebase cannot afford it.

        The docstring here used to answer that with "the standalone audit
        catches any leak". There is no such audit: what it named is the
        page-set design (module docstring, REDACTION IS COMPUTED ONCE), which
        guarantees that the site never LINKS to a page it did not build - it
        has nothing to say about whether a page should have been built. The
        missing marker corrupts the page set itself, so the thing offered as
        the backstop is the very thing that was wrong. A mitigation that does
        not hold is worse than none: it stops the next reader looking.

        Withholding, not failing the build: the page set stays internally
        consistent (the pid never enters `person_pages`, so every link site -
        `render_token`, the charts, the trees, the home index - renders the
        redaction label instead, exactly as for any restricted person), so
        there is no symmetry to break. Publishing less is always safe;
        publishing a name is not undoable. The warning names the file so the
        human can fix it and rebuild.

        An unreadable SOURCE record is handled the same way and covers its
        claims with it: the per-claim `restricted:` markers live in that file
        too, so there is no way to know which claims were withheld. Marking
        the whole source restricted (`restricted_sources`) makes it
        hard-restricted, which drops its claims from every person, place, and
        timeline view - the same reach `restricted_claims` would have had, and
        the only honest one when the claim list itself is unreadable.

        Know what an unreadable PERSON record costs, because it is more than
        one page: `prepare()` also withholds every source that names a
        restricted person (`restricted_person_sources`), so one file that will
        not open can take a visible slice of the site with it. That is the
        existing rule for `restricted: by-request`, applied consistently - and
        it is the right size of consequence for "I could not read this
        person's file", which is why the warning names the file rather than
        just counting.

        One leak this cannot close: a RESTRICTED name variant (a deadname) of
        an unreadable person. Those values live in the same file, and the
        index stores them mangled on purpose, so a `[[deadname]]` written in
        someone else's prose resolves to nothing and renders as the literal
        text. Withholding the person does not help - the name never reaches
        `restricted_names` to be recognised. Only repairing the file does, and
        the warning says so."""
        for pid, row in self.person_meta.items():
            if not row['path']:
                continue
            try:
                rec = read_record(self.archive_root / row['path'],
                                   on_decode_error=_raise_friendly_decode_error)
                trouble = rec['parse_errors'][0][1] if rec['parse_errors'] else None
            except Exception as e:   # noqa: BLE001 - any failure is the same failure here
                rec, trouble = None, str(e)
            if trouble is not None:
                self.restricted_persons.add(pid)
                self.messages.append(
                    f'WARNING: could not read {row["path"]} ({trouble}), so there '
                    f'is no way to tell whether that person asked to be left out '
                    f'of public output. They have been withheld from this site '
                    f'- no page, and their name shown as "{_LIVING_LABEL}" '
                    f'everywhere it would have appeared. Fix or restore that '
                    f'file and run `fha site` again.'
                )
                continue
            meta = rec['meta']
            if _is_restricted_value(meta.get('restricted')):
                self.restricted_persons.add(pid)
            for v in meta.get('name_variants') or []:
                if isinstance(v, dict) and _is_restricted_value(v.get('restricted')):
                    value = v.get('value')
                    if value:
                        self.restricted_names.setdefault(pid, set()).add(str(value).strip().lower())
        for sid, row in self.source_meta.items():
            if row['restricted'] or not row['path']:
                continue   # index-restricted sources are already handled
            try:
                rec = read_record(self.archive_root / row['path'],
                                   on_decode_error=_raise_friendly_decode_error)
                trouble = rec['parse_errors'][0][1] if rec['parse_errors'] else None
            except Exception as e:   # noqa: BLE001 - any failure is the same failure here
                rec, trouble = None, str(e)
            if trouble is not None:
                self.restricted_sources.add(sid)
                self.messages.append(
                    f'WARNING: could not read {row["path"]} ({trouble}), so there '
                    f'is no way to tell whether that source - or any fact in it - '
                    f'was marked private. It has been withheld from this site, '
                    f'and so has everything it is the evidence for. Fix or '
                    f'restore that file and run `fha site` again.'
                )
                continue
            if _is_restricted_value(rec['meta'].get('restricted')):
                self.restricted_sources.add(sid)
            for claim in rec['claims']:
                if not isinstance(claim, dict):
                    continue
                cid = normalize_id(str(claim.get('id', '')))
                if cid and _is_restricted_value(claim.get('restricted')):
                    self.restricted_claims.add(cid)

    def _load_open_questions(self) -> None:
        """Index every OPEN '## Q:' block by the person id(s) its `refs:`
        names, so build_person_page can look a person's questions up by pid
        instead of re-scanning the whole log per page (issue #117).

        `_lib.parse_questions` reads both notes/questions.md AND every
        person's own research-file `## Open Questions` section - the same
        parser `fha report` reads, so the two tools never disagree about
        what a person's open questions are. That scope answers a design
        question the issue leaves open: a question logged in Person B's
        research file that also `refs:` Person A DOES surface on Person A's
        page, not just on Person B's - inherited for free from the shared
        parser's existing reach, not a new decision made here.

        Only 'open' questions are indexed - an answered or closed '## Q:'
        is settled research, not a live pointer a page should keep raising.
        A widely-referenced question naming several people legitimately
        appears on several pages; that is not a duplication bug.

        Called only when self.linked (see build_person_page): a '## Q:'
        block carries no `restricted:` field of its own yet, and its
        `context:` can legitimately hold sensitive detail about a living
        third party that nothing here has vetted - but `--linked` is
        already this codebase's boundary for exactly that kind of
        real-but-unvetted content (the same one `drop_private=not
        self.linked` and `_person_is_redacted` rely on), so this stays off
        the standalone/public build only, not off every unredacted one.

        ONE exception to that "unredacted" bargain: a question block whose
        HOME file is a `restricted: by-request` person's own research
        companion is excluded here entirely - never filed under any pid,
        including that person's own. `by-request` is SPEC §19's one
        no-override restriction ("honored by every export path with no
        opt-in"), stronger than the plain `restricted`/`living` gates every
        other check in this class relaxes under `--linked` (that relaxation
        is a deliberate, tested design - `UnreadableRecordPrivacyTests.
        test_linked_mode_is_unchanged` pins it for a person's own
        name/vitals/bio - not something this fix reopens). What is new and
        NOT covered by that precedent: a `## Q:` block is not the
        by-request person's own record content, it is private research
        ABOUT them, and `refs:` can fan it out onto a DIFFERENT,
        unrestricted relative's page who never asked for any of
        `--linked`'s reduced redaction. See `_question_origin_is_by_request`.

        THE OTHER DIRECTION of that same exception (adversarial review of
        #117): the origin check above only ever asks about the HOME file's
        own owner - it says nothing about a question that is homed in an
        ordinary, unrestricted person's research file but also `refs:` a
        DIFFERENT person who herself asked to be left out. Filing under
        `refs:` fans exactly that question onto her page too, so each ref is
        checked on its own via `_person_is_by_request` right below, in
        addition to (not instead of) the origin check above - a by-request
        person's private research stays off her own page whether the leak
        would have come from HER file being the home, or from someone
        ELSE's file merely mentioning her.

        Also guards the read itself: a research file (or notes/questions.md)
        saved in a non-UTF-8 encoding used to raise `UnicodeDecodeError`
        straight out of `_lib.parse_questions` and take the whole `fha site
        --linked` build down with it (that function only caught `OSError`).
        `on_decode_error` turns that into a per-file skip-and-warn instead -
        every other file's questions still index normally.
        """
        undecodable: list[Path] = []
        for _key, info in sorted(
                parse_questions(self.archive_root, on_decode_error=undecodable.append).items()):
            if info['status'] != 'open':
                continue
            if self._question_origin_is_by_request(info):
                continue
            # set(): a `refs:` list naming the same person twice (a plausible
            # copy-paste slip, e.g. `refs: [P-x, P-x]`) must not double-file
            # the question onto that one person's own list, which would
            # render it twice on their page - a real duplication bug, unlike
            # the same question legitimately appearing on SEVERAL people's
            # pages.
            for ref in {r for r in info['refs'] if r.startswith('p-')}:
                # Per-ref check (adversarial review of #117, see this
                # method's docstring): the origin check above only ever
                # covers the HOME file's own owner - a question homed in an
                # unrestricted person's file can still name a DIFFERENT,
                # by-request person here, and only checking each ref
                # individually catches that direction of the same leak.
                if self._person_is_by_request(normalize_id(ref)):
                    continue
                self.person_questions.setdefault(ref, []).append(info)
        for path in undecodable:
            try:
                rel = path.relative_to(self.archive_root).as_posix()
            except ValueError:
                rel = path.as_posix()
            self.messages.append(
                f"WARNING: could not read {rel} (this file isn't saved as "
                "UTF-8 text - a Windows editor's default encoding, often "
                "cp1252, is the usual cause); its open questions are "
                'omitted from this build - every other page is unaffected. '
                'Open it and save it again choosing UTF-8, then run `fha '
                'site` again.'
            )

    def _question_origin_is_by_request(self, info: dict) -> bool:
        """True when `info`'s home file is a person's own research companion
        and that person's OWN profile carries `restricted: by-request`.

        `notes/questions.md` names no single person - it is never filtered
        here, only a research-file-homed question is. The origin pid is read
        straight off the companion's filename (SPEC §13:
        `{surname}__{given}[_research]_{P-id}.md` - the trailing id is
        unambiguous even when the kind slot itself is, `parse_filename`'s own
        `kind_ambiguous` case), not off `self.restricted_persons`: that set
        is filled by `_load_restriction_markers`, which `prepare()` never
        calls when `self.linked` - and `_load_open_questions` runs precisely
        when `self.linked` - so it is always empty here. Cross-checked
        against the file's own frontmatter `id:` by
        `_origin_frontmatter_id_mismatches` (adversarial review of #117) -
        see that method for why trusting the filename alone is not enough on
        its own when the two disagree.

        This only ever gates the ORIGIN (home) file - the by-request check
        that matters for a specific `refs:` target is a separate, per-pid
        check `_load_open_questions` applies to each ref individually (see
        its own comment): a question homed in an unrestricted person's file
        can still `refs:` a DIFFERENT, by-request person, and this method
        alone would never catch that - it only ever asks about the file's
        own owner.
        """
        file_rel = info.get('file') or ''
        if file_rel == 'notes/questions.md':
            return False
        parsed = parse_filename(Path(file_rel))
        if parsed is None or parsed.get('id_type') != 'P':
            return False   # not a person research file - nothing to check
        pid = normalize_id(parsed['id_str'])
        if pid not in self._by_request_origin_cache:
            self._by_request_origin_cache[pid] = (
                self._person_is_by_request(pid)
                or self._origin_frontmatter_id_mismatches(file_rel, pid)
            )
        return self._by_request_origin_cache[pid]

    def _origin_frontmatter_id_mismatches(self, file_rel: str, filename_pid: str) -> bool:
        """True when `file_rel`'s own frontmatter `id:` disagrees with (or
        cannot be read against) the P-id its FILENAME claims - defense in
        depth for `_question_origin_is_by_request` (adversarial review of
        #117).

        A research companion's filename and its own `id:` field are SUPPOSED
        to always agree (`archive-template/people/_TEMPLATE.research.md`:
        "the person's code, same as on their profile") - SPEC §13's self-
        identifying-filename invariant, and `fha lint`'s E003 already flags
        a mismatch as an archive-level error the owner is expected to fix.
        But while that inconsistency sits unresolved (a hand-rename or a
        copied file, `id:` and filename edited independently), trusting the
        filename alone to answer "does this by-request check apply to the
        right person" means a mismatched file can no longer be told apart
        from an ordinary one by name alone - the wrong person's by-request
        status governs the real one's private research. Fails CLOSED the
        same way `_person_is_by_request` already does for its own other
        "can't verify" cases (unreadable, undecodable, no profile), rather
        than trusting either signal on its own when they disagree.
        """
        try:
            rec = read_record(self.archive_root / file_rel, on_decode_error=_ignore_decode_error)
            trouble = rec['parse_errors'][0][1] if rec['parse_errors'] else None
        except Exception as e:   # noqa: BLE001 - any failure is the same failure here
            rec, trouble = None, str(e)
        if trouble is not None or (rec is not None and rec.get('undecodable')):
            self.messages.append(
                f'WARNING: could not read {file_rel} to confirm it really belongs to '
                f"{fmt_id_display(filename_pid)} (its filename) before publishing its "
                'open questions - withheld rather than shown without being able to check.'
            )
            return True
        frontmatter_id = normalize_id(str(rec['meta'].get('id') or ''))
        if frontmatter_id != filename_pid:
            self.messages.append(
                f"WARNING: {file_rel}'s filename names {fmt_id_display(filename_pid)} but "
                f"its own `id:` says "
                f"{fmt_id_display(frontmatter_id) if frontmatter_id else '(none)'} - "
                "`fha lint`'s E003 flags this mismatch; its open questions are withheld "
                'rather than shown under whichever person turns out to be wrong. Fix the '
                'mismatch (see `fha lint`) and run `fha site` again.'
            )
            return True
        return False

    def _person_is_by_request(self, pid: str) -> bool:
        """Read one person's OWN profile record and say whether it carries
        `restricted: by-request` - independent of `self.linked` and of
        `self.restricted_persons` (see `_question_origin_is_by_request`).

        Fails CLOSED like `_load_restriction_markers` does for the exact same
        reason (see its docstring): a pid this build cannot resolve to a
        readable profile - no person row, no path, an unreadable or
        undecodable file - is treated as if it HAD asked to be left out
        rather than shown without being able to check, because a missing
        marker reads exactly like a person who never asked. Each failure is
        still reported by name, never silent.
        """
        row = self.person_meta.get(pid)
        if row is None or not row['path']:
            self.messages.append(
                f'WARNING: an open question is filed under {fmt_id_display(pid)}\'s '
                "research companion, but no readable profile record exists for "
                "that id - it is being treated as if that person had asked to "
                "be left out (restricted: by-request), so its open questions "
                "are withheld rather than shown without being able to check."
            )
            return True
        try:
            rec = read_record(self.archive_root / row['path'], on_decode_error=_ignore_decode_error)
            trouble = rec['parse_errors'][0][1] if rec['parse_errors'] else None
        except Exception as e:   # noqa: BLE001 - any failure is the same failure here
            rec, trouble = None, str(e)
        if trouble is not None or (rec is not None and rec.get('undecodable')):
            name = row['name'] or fmt_id_display(pid)
            self.messages.append(
                f'WARNING: could not read {row["path"]} to check whether '
                f'{name} asked to be left out (restricted: by-request) before '
                "publishing their open questions - withheld rather than shown "
                'without being able to check. Fix or restore that file and '
                'run `fha site` again.'
            )
            return True
        return _restricted_type(rec['meta'].get('restricted')) == 'by-request'

    def _open_photos(self) -> None:
        """Open the photo index once if it is fresh, for the person photo strips.

        The freshness check (`photoindex_status`) walks the whole photos root,
        so it must run once per build - never once per person. An absent, stale,
        or unreadable photo index simply leaves `self.photos_conn` None and the
        photo strip is omitted (it is enrichment, never a build blocker).

        The status is kept on `self.photos_status` because one thing reading
        this catalog is NOT enrichment: the living-person photo gate. It fails
        open, so the build warns once and names which of these states stopped it
        (`_warn_living_photo_check_unavailable`).
        """
        status, _lag = photoindex_status(self.archive_root, self.fha_config)
        self.photos_status = status
        if status != 'fresh':
            return
        try:
            conn = sqlite3.connect(str(self.archive_root / '.cache' / 'photos.sqlite'))
            conn.row_factory = sqlite3.Row
            self.photos_conn = conn
        except sqlite3.DatabaseError:
            self.photos_conn = None
            self.photos_status = 'unreadable'

    def close(self) -> None:
        """Close any auxiliary connection this build opened (the index
        connection itself is owned and closed by run_site)."""
        if self.photos_conn is not None:
            self.photos_conn.close()
            self.photos_conn = None

    def _source_is_redacted(self, row: sqlite3.Row) -> bool:
        """A source is withheld from a standalone snapshot when restricted, DNA,
        or explicitly `rights.publication_ok: false` (TOOLING §12 / SPEC §21).
        `COALESCE(publication_ok, 1) = 0` is the codebase-wide predicate (gedcom,
        wikitree): absent → publishable, explicit false → withheld. A free-text
        restricted type (`restricted: by-request`) the index stored as 0 is
        caught via the `restricted_sources` set read from the record files."""
        if (row['restricted'] or 0):
            return True
        if row['id'] in self.restricted_sources:
            return True
        if (row['source_type'] or '') == 'dna':
            return True
        pub = row['publication_ok']
        return pub is not None and int(pub) == 0

    def _source_hard_restricted(self, sid: str | None) -> bool:
        """A source that is *intentionally* private - restricted / DNA / by-request
        / publication_ok:false - as opposed to one merely withheld from the
        snapshot because it names a living person. Hard-restricted material stays
        hidden; a merely-withheld source's facts about the deceased may still show,
        with the citation redacted (only living people are redacted outright)."""
        if not sid:
            return False
        # A source named as evidence for a `restricted: by-request` person is
        # also intentionally private - publishing its facts (even with the
        # citation redacted) would leak the deceased person's material.
        if sid in self.restricted_person_sources:
            return True
        row = self.source_meta.get(sid)
        return row is not None and self._source_is_redacted(row)

    def _person_is_redacted(self, row: sqlite3.Row) -> bool:
        """A person is redacted from standalone output when living/unknown
        (AGENTS.md; `unknown` is treated as living) or `restricted` (any value,
        SPEC §21 - a restricted person, like a living one, gets no page and is
        rendered as a redaction label)."""
        if row['id'] in self.restricted_persons:
            return True
        return (row['living'] or '') in ('true', 'unknown')

    def _name_is_sensitive(self, key: str) -> bool:
        """True when a lowercased, wrapper-stripped name-link key must not be
        rendered verbatim on the standalone site: it resolves (ambiguously) to a
        living/restricted person, or it is a restricted variant (deadname) of
        some person. The clash-aware alias_map drops such names, so without this
        check render_token would fall through and publish the literal name."""
        for cid in self._alias_candidates.get(key, ()):  # type: ignore[union-attr]
            meta = self.person_meta.get(cid)
            if meta is not None and self._person_is_redacted(meta):
                return True
        return any(key in values for values in self.restricted_names.values())

    # - token rendering -

    def render_token(self, token: str, page_dir: Path, in_display: str | None = None) -> str:
        """Render one citation token to HTML, relative to the page being built.

        `token` is an ID *or* a human name/stem; a name/stem is resolved through
        the alias map first. `in_display` is the text a human wrote inside the
        token (`[[P-id|Margaret Cole]]`) and is preferred over the resolved
        record name - EXCEPT for a redacted living person, where neither the name
        nor the in-token display is ever emitted.

        P-id → link to the person page when one exists; "Living Person" when the
        person is redacted under standalone; otherwise the plain (escaped) name.
        S-id → link to the source page, or "Restricted - not included…" when
        withheld. L-id → link to the place page (places are never redacted).
        A dangling *ID* token renders highlighted - `<mark>[X-xxxx]</mark>` (TOOLING
        §12 / BUILD M8.1; already a lint error). An unresolved *name/stem* link is
        an ordinary Obsidian note-link, not a citation, and renders as plain text.
        """
        pid = normalize_id(token)
        kind = id_type_of(pid)
        if kind is None:
            # A name/stem wikilink target - resolve through the alias map.
            resolved = self.alias_map.get(strip_link_wrapper(token).lower())
            if resolved:
                pid, kind = resolved, id_type_of(resolved)
            else:
                # Inert Obsidian link, not a broken citation → plain text.
                # Exception: in standalone mode fail closed rather than leak a
                # name when we can't be sure it's inert - the aliases table is
                # absent (stale index), OR the target/display is an ambiguous
                # name that clashes onto a living/restricted person or is a
                # restricted variant (deadname). Those are dropped from
                # alias_map, so they land here unresolved (SPEC §21).
                if not self.linked and (
                        not self._alias_table_ok
                        or self._name_is_sensitive(strip_link_wrapper(token).lower())
                        or (in_display
                            and self._name_is_sensitive(in_display.strip().lower()))):
                    return f'<span class="redacted">{_LIVING_LABEL}</span>'
                return _escape(in_display or token)

        display = fmt_id_display(pid)
        in_display = (in_display or '').strip() or None

        if kind == 'P' and pid in self.person_meta:
            row = self.person_meta[pid]
            if not self.linked and self._person_is_redacted(row):
                return f'<span class="redacted">{_LIVING_LABEL}</span>'
            # A restricted name (a deadname) resolves to the person internally
            # but must never be displayed: drop an in-token display that is one of
            # this person's restricted variants, so the unrestricted display name
            # is shown instead (SPEC §18).
            if (not self.linked and in_display
                    and in_display.strip().lower() in self.restricted_names.get(pid, set())):
                in_display = None
            name = _escape(in_display or row['name'] or display)
            if pid in self.person_pages:
                href = html.escape(_rel_href(self.persons_dir / _page_filename(pid), page_dir), quote=True)
                return f'<a href="{href}">{name}</a>'
            # A person without a page (a stub): show the name but mark it a stub
            # with a dotted underline, mirroring the tree's stub-node convention.
            # (In the workbench stubs have pages, so this branch is only reached
            # outside it - no affordance to attach.)
            return f'<span class="stub-ref">{name}</span>'
        if kind == 'S' and pid in self.source_meta:
            return self._cite_source(pid, page_dir, in_display)
        if kind == 'C':
            # A claim reference cites its backing source (never a raw claim id in
            # the reading view). An unresolvable claim simply drops out.
            sid = self._claim_source(pid)
            return self._cite_source(sid, page_dir) if sid else ''
        if kind == 'L' and pid in self.place_meta:
            name = _escape(in_display or self.place_names.get(pid) or display)
            if pid in self.place_pages:
                href = html.escape(_rel_href(self.places_dir / _page_filename(pid), page_dir), quote=True)
                return f'<a href="{href}">{name}</a>'
            return name
        # A dangling source id is a citation with nothing to point at: hide it
        # rather than print a backend id into the reading view (lint flags it).
        if kind == 'S':
            return ''
        # Unresolved ID token - surfaced as the literal [X-xxxx] form, not hidden
        # (TOOLING §12 / BUILD M8.1; these are already lint errors). Workbench:
        # a claim-named person with no record yet gets the wireframe's mint '+'
        # right where their reference appears - person.new reuses the P-id
        # passed via data-wb-args, so every claim naming them keeps pointing
        # at the same person once the stub exists.
        if self.workbench and kind == 'P':
            args = html.escape(json.dumps({'person_id': display}), quote=True)
            return (f'<mark>[{_escape(display)}]</mark>'
                    f'<a class="wb-mint" href="#" data-wb-open="tpl-mint" '
                    f"data-wb-args='{args}' "
                    f'title="Create a record for {_escape(display)}">+</a>')
        return f'<mark>[{_escape(display)}]</mark>'

    def _person_link(self, pid: str, page_dir: Path) -> str:
        """A bare person reference (not from prose) → link / redaction / name."""
        return self.render_token(fmt_id_display(pid), page_dir)

    def _footnote_number(self, sid: str) -> int:
        """The stable footnote number for a source on the current person page,
        assigning the next one on first citation (repeated cites reuse it)."""
        sid = normalize_id(sid)
        assert self._footnotes is not None
        if sid not in self._footnotes:
            self._footnote_seq.append(sid)
            self._footnotes[sid] = len(self._footnote_seq)
        return self._footnotes[sid]

    def _claim_source(self, cid: str) -> str | None:
        """The source id backing a claim, or None (used to cite a `[C-id]` token
        by its source rather than printing the raw claim id)."""
        try:
            row = self.conn.execute(
                'SELECT source_id FROM claims WHERE id = ?', (normalize_id(cid),)).fetchone()
        except sqlite3.DatabaseError:
            return None
        return row['source_id'] if row and row['source_id'] else None

    def _cite_source(self, sid: str | None, page_dir: Path, in_display: str | None = None) -> str:
        """One source citation, used everywhere a source is referenced.

        On a person page (a footnote registry is active): a small superscript
        number into the numbered Sources list - so dense facts stay legible and
        repeated sources collapse to one number - or, for a withheld source, the
        single shared 'Restricted' footnote (its identity/count never leaks, and
        the label never repeats inline). A source the author *named* in prose
        (`[[S-id|text]]`) stays a plain link. Off a person page (place pages), a
        named link or the redacted label. Dangling → nothing."""
        if not sid:
            return ''
        sid = normalize_id(sid)
        row = self.source_meta.get(sid)
        if row is None:
            return ''
        restricted = not self.linked and sid not in self.source_pages
        if self._footnotes is not None:
            key = _RESTRICTED_FN if restricted else sid
            n = self._footnote_number(key)
            if restricted or not in_display:
                return f'<sup class="fn-ref"><a href="#fn-{n}">{n}</a></sup>'
            href = html.escape(_rel_href(self.sources_dir / _page_filename(sid), page_dir), quote=True)
            return f'<a href="{href}">{_escape(in_display)}</a>'   # author named it in prose
        if restricted:
            return f'<span class="redacted">{_RESTRICTED_LABEL}</span>'
        title = _escape(in_display or row['title'] or fmt_id_display(sid))
        if sid in self.source_pages:
            href = html.escape(_rel_href(self.sources_dir / _page_filename(sid), page_dir), quote=True)
            return f'<a href="{href}">{title}</a>'
        return title

    def _source_link(self, sid: str, page_dir: Path) -> str:
        """Compact source citation for summary/timeline rows (see `_cite_source`)."""
        return self._cite_source(sid, page_dir)

    def _place_label(self, place_text: str | None, place_id: str | None) -> str:
        """Place display string: the as-written text, else the registry name."""
        if place_text:
            return place_text
        if place_id and place_id in self.place_names:
            return self.place_names[place_id]
        return ''

    def _place_html(self, place_text: str | None, place_id: str | None, page_dir: Path):
        """Place cell for claims tables / timelines: the display text linked to
        its place page when the claim carries a registered `place_id`, else plain
        text (free-text `place_text` with no registry id stays unlinked). Returns
        a Markup so the template renders the link; an empty Markup when there is
        no place at all (so `{% if %}` guards still treat it as absent). Mirrors
        the `[L-id]`-token link in prose - the symmetry the review flagged.

        `label` is scrubbed the same way every other reader-facing free-text
        field on this page already is (#140, adversarial review round 4
        audit): `place_text` is ordinary hand-typed prose - "Millbrook,
        Dutchess County, New York" - not exempt from the same accidental
        leak `_scrub_internal_encoding` exists to catch everywhere else (a
        pasted-in bare citation id, an unedited `[..YYYY]` bracket). This
        was the one reader-facing free-text field on this page that never
        ran through it - `value_raw`/`place_text_raw`'s WORKBENCH edit-form
        siblings deliberately stay unscrubbed (the human must see their real
        stored text to edit it), but this is the read-only render, not an
        edit form, so it gets no such exemption."""
        label = _scrub_internal_encoding(self._place_label(place_text, place_id))
        if not label:
            return self._markup('')
        if place_id and place_id in self.place_pages:
            href = html.escape(_rel_href(self.places_dir / _page_filename(place_id), page_dir), quote=True)
            return self._markup(f'<a href="{href}">{_escape(label)}</a>')
        return self._markup(_escape(label))

    def _timeline_value_html(self, value: str, mention: tuple[int, int] | None,
                             place_id: str | None, page_dir: Path):
        """A timeline sentence as HTML, with the place it already names linked.

        #127 stops the timeline repeating a place the sentence has already
        stated, but that trailing place was also the only thing linking the
        place's page from a person page - the link symmetry `_place_html`
        exists for, and it would go missing for exactly the claims most
        likely to carry a registered place ("Thomas Hartley born ... in
        Fairview, Breton County, Kansas"). So when the sentence carries the
        mention, the mention carries the link: the place page stays one click
        away and its name is still printed only once.

        Escaping happens here at the leaves (the same rule as every other
        `_markup` caller in this file), since the returned Markup goes to the
        template with autoescape already satisfied."""
        if mention is None or not place_id or place_id not in self.place_pages:
            return self._markup(_escape(value))
        start, end = mention
        href = html.escape(_rel_href(self.places_dir / _page_filename(place_id), page_dir), quote=True)
        return self._markup(f'{_escape(value[:start])}<a href="{href}">'
                            f'{_escape(value[start:end])}</a>{_escape(value[end:])}')

    # - assets -

    def _asset_href(self, resolved: Path, page_dir: Path) -> str:
        """Href for an on-disk ASSET file (a photo/document/inbox scan), honoring
        workbench mode.

        In plain linked mode this is exactly `_rel_href` - a `../../` relative
        path from the page directory to the real file, which works when the site
        is opened from a file browser. But serve delivers the snapshot over HTTP
        from `.cache/serve/site/`, and a `../../photos/...` link would climb out
        of the snapshot root and 404 (or worse, escape confinement). So in
        workbench mode any asset that lives under an allowed asset root
        (photos/documents/inbox) is rewritten to serve's read-only
        `/root/<alias>/<relpath>` URL; anything else (an asset root configured
        somewhere exotic) falls back to the relative href rather than emitting a
        broken link. The rewrite is applied ONLY in workbench mode, so `fha site
        --linked` keeps its file-browser-relative behavior untouched."""
        if self.workbench:
            alias_url = self._root_alias_url(resolved)
            if alias_url is not None:
                return alias_url
        return _rel_href(resolved, page_dir)

    def _root_alias_url(self, resolved: Path) -> str | None:
        """Map an absolute asset path to serve's `/root/<alias>/<relpath>` URL, or
        None when it is not under any allowed asset root.

        Mirrors serve's own `_resolve_root_request` confinement (photos,
        documents, inbox only) so a href serve emits is one serve will also
        serve: resolve each allowed root, and if `resolved` sits under it, build
        a forward-slash URL from the relative remainder.

        Each path segment is percent-encoded (`#`/`?`/space and friends) -
        serve's handler already `unquote()`s the whole `/root/...` path
        before splitting alias from relpath, but a literal `#`/`?` in an
        UNencoded href is stripped by the BROWSER before the request is even
        sent (a URL fragment/query, not part of the path), so the request
        that reaches serve is silently truncated and 404s even though the
        file exists. `safe='/'` keeps the path separators themselves
        unescaped."""
        try:
            target = resolved.resolve()
        except OSError:
            return None
        for alias in ASSET_ROOT_ALIASES:
            try:
                base = resolve_path(alias, self.fha_config, self.archive_root).resolve()
            except Exception:
                continue
            try:
                rel = target.relative_to(base)
            except ValueError:
                continue
            rel_posix = _urlquote(rel.as_posix(), safe='/')
            return f'/root/{alias}/{rel_posix}' if rel_posix != '.' else f'/root/{alias}'
        return None

    def _file_entry(self, asset_rel: str, role: str | None, copy: str | None, date_edtf: str | None, page_dir: Path) -> dict | None:
        """Build one source-page file entry (thumbnail + link) for an asset.

        Returns None for an asset that should not appear at all. The resolved
        on-disk path is found through `fha.yaml` roots (an asset root may live
        outside the archive). Behavior by mode:
          - missing on disk → a named note, no link (common with fixture stubs).
          - --linked → link straight at the real file (and, for an image, use it
            as its own thumbnail); nothing is copied.
          - --standalone, image, PIL present → write an EXIF-stripped derivative
            into media/{sid}; link + thumbnail that. PIL absent or derivative
            failed → omit the image with a note (never copy the original, which
            would leak EXIF).
          - --standalone, non-image → list the filename with a note that the
            original stays in the archive (originals never leave - TOOLING §12).

        `copy` (the `files:` entry's optional `copy: b`/`c`/`d` variant
        letter, SPEC §14 - now that #123 fixed the indexer landing it as
        NULL) rides along in the note so a bundle of same-day or same-source
        variants (front/back copy A, front/back copy B, …) reads as
        distinguishable entries instead of a wall of identical "role: front"
        labels. `date_edtf` (the entry's optional per-file `date:`, SPEC §14,
        #123) rides along the same way, rendered human-readable and first.

        Every branch below is a DEGRADED display, not a missing record: the
        index still knows the file's date/role/copy even when the asset
        itself cannot be shown (path unresolvable, missing on disk, Pillow
        absent). `role_note` is computed up front from the parameters alone -
        it needs nothing the resolve step below can fail to produce - and
        `_with_role_note` composes it onto every fallback message, so those
        facts never silently vanish just because the FILE became
        unpresentable (#123 follow-up, Codex review on PR #149: this used to
        replace the whole note with a fixed message on each of these paths)."""
        label = Path(asset_rel).name
        role_note = _role_note(role, copy, date_edtf)
        try:
            resolved = resolve_path(asset_rel, self.fha_config, self.archive_root)
        except Exception:
            return {'label': label, 'note': _with_role_note('asset path could not be resolved', role_note),
                    'link_href': None, 'thumb_href': None}
        is_image = resolved.suffix.lower() in _IMAGE_SUFFIXES

        if not resolved.exists():
            return {'label': label, 'note': _with_role_note('file not available in this build', role_note),
                    'link_href': None, 'thumb_href': None}

        if self.linked:
            href = self._asset_href(resolved, page_dir)
            return {
                'label': label, 'note': role_note,
                'link_href': href,
                'thumb_href': href if is_image else None,
            }

        # standalone
        if is_image:
            if not _PIL_AVAILABLE:
                return {'label': label, 'note': _with_role_note('image omitted (Pillow not installed)', role_note),
                        'link_href': None, 'thumb_href': None}
            return None  # signal: caller handles derivative creation (needs sid)
        return {'label': label, 'note': _with_role_note('original kept in the archive', role_note),
                'link_href': None, 'thumb_href': None}

    def _media_dest(self, alias_path: str, subdir: str) -> Path:
        """Collision-free derivative path under media/{subdir}.

        Two assets can share a filename stem across different folders (scan
        archives often reuse per-folder sequential names like `001.jpg`).
        Namespacing by stem alone would let the second overwrite the first and
        publish the wrong image. A short hash of the full alias path makes the
        name unique while staying deterministic - the same asset always maps to
        the same derivative, so it is built once and reused across pages rather
        than churning or colliding."""
        norm = alias_path.replace('\\', '/')
        digest = hashlib.sha1(norm.encode('utf-8')).hexdigest()[:8]
        return self.media_dir / subdir / f'{Path(norm).stem}_{digest}.jpg'

    def _standalone_image_entry(self, sid: str, asset_rel: str, role: str | None, copy: str | None, date_edtf: str | None, page_dir: Path) -> dict:
        """Create the media derivative for a standalone image asset and return
        its file entry. Split from `_file_entry` because it needs the source id
        for the media subfolder and may emit a warning into `self.messages`.
        `copy`/`date_edtf` mirror `_file_entry`'s parameters of the same name
        (SPEC §14). On a failed derivative (corrupt image, unsupported format,
        locked file) the date/role/copy note still carries through via
        `_with_role_note` rather than being replaced outright (#123 follow-up,
        Codex review on PR #149) - the FILE could not be rendered, but the
        indexed facts about it are still known and worth showing."""
        resolved = resolve_path(asset_rel, self.fha_config, self.archive_root)
        dest = self._media_dest(asset_rel, normalize_id(sid))
        role_note = _role_note(role, copy, date_edtf)
        if _make_derivative(resolved, dest):
            href = _rel_href(dest, page_dir)
            return {'label': Path(asset_rel).name, 'note': role_note,
                    'link_href': href, 'thumb_href': href}
        self.messages.append(f'WARNING: could not build a web image for {asset_rel} (skipped, build continues)')
        return {'label': Path(asset_rel).name, 'note': _with_role_note('image could not be processed', role_note),
                'link_href': None, 'thumb_href': None}

    # - source page (M8.1) -

    def build_source_page(self, sid: str) -> None:
        """Render one source page: citation, metadata, claims table, files.

        Wrapped so a single malformed source never aborts the build - its page
        falls back to the title-only citation and a plain warning, and the rest
        of the site still renders (M8 UX bar (a)+(c)).
        """
        row = self.source_meta[sid]
        page_dir = self.sources_dir

        citation = ''
        record_body = ''
        try:
            rec = read_record(self.archive_root / row['path'],
                               on_decode_error=_raise_friendly_decode_error)
            citation = ' '.join(str(rec['meta'].get('citation', '') or '').split())
            record_body = rec.get('body') or ''
            if rec['parse_errors']:
                self.messages.append(
                    f'WARNING: {row["path"]} has a formatting problem '
                    f'({rec["parse_errors"][0][1]}); showing the title in place of its citation.'
                )
        except Exception as e:
            self.messages.append(f'WARNING: could not read {row["path"]} ({e}); showing the title only.')

        # Workbench: the record's ## Notes section, rendered under the page's
        # Research Notes heading - the wireframe shows the notes a source-note
        # apply writes; without this the just-written note is invisible on
        # reload and the apply reads as a silent failure. Rendered per entry
        # (same split as the person page's Stories/Research logs) so each
        # note carries its own edit button; notes_html stays as the joined
        # fallback for any template path that wants the section whole.
        notes_html = None
        notes_entries: list[dict] = []
        if self.workbench and record_body:
            m = re.search(r'^## Notes[ \t]*\r?$\n(.*?)(?=^## |\Z)', record_body,
                          re.M | re.S)
            notes_text = (m.group(1).strip() if m else '')
            if notes_text:
                render = lambda tok, disp=None: self.render_token(tok, page_dir, disp)  # noqa: E731
                embed = lambda t, c: self._render_embed(t, c, page_dir)  # noqa: E731
                notes_html = self._markup(_prose_to_html(
                    notes_text, render, embed, drop_private=not self.linked))
                notes_entries = [
                    {'html': self._markup(_prose_to_html(
                        e, render, embed, drop_private=not self.linked)), 'raw': e}
                    for e in split_log_entries(notes_text)]

        # A standalone snapshot publishes only settled facts - accepted claims
        # (any confidence; the badge carries a low-confidence indicator).
        # `needs-review` is research state ("looked at it, can't settle it
        # yet" - the parked verdict, SPEC §8.1), not publishable fact, so it
        # is withheld from public output along with `suggested` (unreviewed
        # AI drafts) and `rejected`/`superseded` (known not current) - owner
        # decision 2026-07-22. `--linked` (developer preview / workbench)
        # shows every status with its badge.
        status_filter = '' if self.linked else "AND status = 'accepted'"
        living_filter = (
            '' if self.linked else
            "AND NOT EXISTS ("
            "  SELECT 1 FROM claim_persons cp2 JOIN persons p ON cp2.person_id = p.id "
            "  WHERE cp2.claim_id = c.id AND p.living IN ('true','unknown')"
            ")"
        )
        claims = []
        for c in self.conn.execute(
            'SELECT id, type, value, date_edtf, place_id, place_text, status, confidence FROM claims c '
            f'WHERE source_id = ? {status_filter} {living_filter} ORDER BY '
            "CASE WHEN date_min IS NULL OR date_min = '' THEN 1 ELSE 0 END, date_min ASC",
            (sid,),
        ):
            # A restricted claim (read from the record file) is withheld from
            # public output even when its source page is published (SPEC §8.4).
            if not self.linked and normalize_id(str(c['id'])) in self.restricted_claims:
                continue
            person_rows = self.conn.execute(
                'SELECT person_id FROM claim_persons WHERE claim_id = ? ORDER BY position', (c['id'],)
            ).fetchall()
            persons_html = ', '.join(self._person_link(p['person_id'], page_dir) for p in person_rows)
            claims.append({
                'type': c['type'],
                # Reader-facing cell: scrubbed of internal-only encoding the
                # same way prose/timeline values already are (#144 finding
                # 4) - a source page is a reader-facing page like any other.
                'value': _scrub_internal_encoding(c['value'] or ''),
                'date': c['date_edtf'] or '',
                'place': self._place_html(c['place_text'], c['place_id'], page_dir),
                'persons_html': self._markup(persons_html), 'status': c['status'],
                'confidence': c['confidence'] or '',
                # Workbench-only: the C-id drives the inline claim actions. Never
                # used in standalone output (the template gates on `workbench`).
                'claim_id': fmt_id_display(c['id']),
                # Workbench-only: raw (not pre-rendered-to-HTML) field values so
                # the "edit & accept" modal can prefill with the claim's current
                # data instead of opening blank (PR #30 gap - the biography
                # editor got this fix, this claim modal never did). place_text
                # is the DISPLAY label (registry name when the claim carries a
                # resolved place_id and no text) so a resolved place never
                # opens as a blank field the human could overwrite unknowingly.
                # value_raw stays UNscrubbed on purpose (#144 finding 4): the
                # edit modal must prefill with the claim's real stored text,
                # not the reader-facing scrub, or a human editing the claim
                # would silently lose the very encoding they might want to
                # correct or keep.
                'value_raw': c['value'] or '',
                'place_text': self._place_label(c['place_text'], c['place_id']),
                'place_id': fmt_id_display(c['place_id']) if c['place_id'] else '',
                'persons_ids': ','.join(fmt_id_display(p['person_id']) for p in person_rows),
            })

        files, portrait_entry = self._source_file_entries(sid, page_dir)
        # The record-head thumbnail (win 2): the same href the Files entry
        # already resolved, just framed as a portrait plate with its own
        # caption - never a second derivative, never a second privacy check.
        portrait = ({'href': portrait_entry['thumb_href'], 'full_href': portrait_entry['link_href'],
                    'caption': 'Open the scan full size'} if portrait_entry else None)

        ctx = {
            'display_id': fmt_id_display(sid), 'title': row['title'] or fmt_id_display(sid),
            'source_type': row['source_type'] or '', 'citation': citation,
            'date': row['date_edtf'] or '', 'repository': row['repository'] or '',
            'source_class': row['source_class'] or '', 'claims': claims, 'files': files,
            'portrait': portrait,
            # Workbench-only (template gates on `workbench`): S-id + record path,
            # the record-bar suggested-claims pointer, and the ## Notes render.
            # as_posix: the index stores the path with the building OS's
            # separators; the rendered page (and the committed example
            # fixtures) must not vary by platform.
            'source_id': fmt_id_display(sid),
            'record_relpath': str(row['path']).replace('\\', '/'),
            'suggested_count': sum(1 for c in claims if c['status'] == 'suggested'),
            'notes_html': notes_html,
            'notes_entries': notes_entries,
        }
        self._write_page(self.sources_dir / _page_filename(sid), 'source.html',
                         {'source': ctx, 'root_prefix': '..'})

    def _source_file_entries(self, sid: str, page_dir: Path) -> tuple[list[dict], dict | None]:
        """Build the file-list entries for a source page, creating standalone
        image derivatives as needed, and pick the record-head portrait
        thumbnail (win 2) out of the same pass.

        Returns `(entries, portrait)`. `portrait` is the `source_files` row
        the head-of-record thumbnail should use: `role: front` if one such
        image resolved to a viewable thumbnail, else the first image in
        `source_files`' own row order (the table's insertion order - there is
        no separate sequence column, so "first" here means whatever order the
        SELECT below already returns). It is None whenever no image asset
        resolved to a thumbnail at all - no image row, every image missing on
        disk, Pillow absent in standalone, or every image gated out for
        naming a living person - so the portrait can never show what the
        Files list itself would have hidden; it reuses that list's own
        entries rather than re-resolving the file, so a missing/omitted image
        degrades identically in both places."""
        entries: list[dict] = []
        candidates: list[tuple[bool, dict]] = []   # (is_front, entry) for resolvable images
        for f in self.conn.execute(
            'SELECT path, role, copy, date_edtf FROM source_files WHERE source_id = ?', (sid,)
        ):
            if not f['path']:
                continue
            # Standalone: skip images co-tagged to a living person in the photo catalog.
            # Same bug shape as _file_entry/_standalone_image_entry's fallback
            # branches (Codex review, PR #149 finding 4): only the FILE's
            # presentability is degraded here (it names a living person), not
            # the indexed date/role/copy facts about it, so the fixed message
            # is composed onto _role_note via _with_role_note rather than
            # replacing it outright.
            if not self.linked and self._is_living_tagged_photo(f['path']):
                entries.append({
                    'label': Path(f['path']).name,
                    'note': _with_role_note('image omitted - tagged to a living person',
                                            _role_note(f['role'], f['copy'], f['date_edtf'])),
                    'link_href': None,
                    'thumb_href': None,
                    'path': f['path'],
                })
                continue
            entry = self._file_entry(f['path'], f['role'], f['copy'], f['date_edtf'], page_dir)
            if entry is None:   # standalone image needing a derivative
                entry = self._standalone_image_entry(
                    sid, f['path'], f['role'], f['copy'], f['date_edtf'], page_dir)
            # Workbench: the archive-relative path drives the per-file 'open'
            # (OS editor via /api/open) affordance the wireframe specifies.
            entry['path'] = f['path']
            entries.append(entry)
            is_image = Path(f['path']).suffix.lower() in _IMAGE_SUFFIXES
            if is_image and entry.get('thumb_href'):
                candidates.append(((f['role'] or '').strip().lower() == 'front', entry))
                # An image is going into a shareable snapshot. If the living-
                # person gate at the top of this loop had no catalog to ask,
                # this is the moment the build learns it published something
                # unchecked - and the only honest moment to say so. Warned here
                # rather than inside the gate itself so a build with nothing but
                # text attachments, or one where Pillow left every image out,
                # raises no alarm about photos it never published.
                if not self.linked and self.photos_status != 'fresh':
                    self._warn_living_photo_check_unavailable()
        portrait = next((e for is_front, e in candidates if is_front), None)
        if portrait is None and candidates:
            portrait = candidates[0][1]
        return entries, portrait

    # - person page (M8.2) -

    def build_person_page(self, pid: str) -> None:
        """Render one person page (TOOLING §12 / M8.2) - curated persons, plus
        stub-tier persons in workbench mode (see the person_pages build)."""
        row = self.person_meta[pid]
        page_dir = self.persons_dir
        self._footnotes = {}          # start this page's source-footnote numbering
        self._footnote_seq = []

        summary = self._person_summary(pid, page_dir)
        (biography_html, stories_html, research_html, biography_raw,
         stories_entries, research_entries) = self._person_prose(row, page_dir)
        timeline = self._person_timeline(pid, page_dir)
        sources = self._person_sources(pid, page_dir)
        family = self._person_family(pid, page_dir)
        photos = self._person_photos(pid, page_dir)
        name = row['name'] or fmt_id_display(pid)
        alt_names, tags = self._person_header_meta(pid, name)
        # One Ahnentafel walk feeds both charts: the horizontal pedigree (subject +
        # parents + grandparents, slots 1-7) and the deeper radial fan. The pedigree
        # is then widened into a family chart with the subject's spouse(s) and
        # children (win 1) - the fan stays ancestors-only (a fan has no natural
        # place to hang a descendant wing).
        ahnen, missing_parent_of = self._build_ahnentafel(pid, _FAN_GENERATIONS, page_dir)
        ped_labels = {n: e for n, e in ahnen.items() if n < 8}
        ped_missing = {n: c for n, c in missing_parent_of.items() if n < 8}
        wings = self._build_family_wings(pid, page_dir)
        has_pedigree = len(ped_labels) > 1 or wings['spouses'] or wings['children']
        pedigree = (self._markup(_render_pedigree_svg(
                        ped_labels, wings['spouses'], wings['children'],
                        missing_parent_of=ped_missing, workbench=self.workbench))
                   if has_pedigree else None)
        # Same condition _render_pedigree_svg uses for its SVG aria-label (a
        # sighted reader on the page and a screen-reader user on the SVG must
        # be told the same truth about what the chart contains): a subject
        # with a recorded spouse or child gets the family-chart heading, an
        # ancestors-only chart gets the honest 'Ancestors' heading instead of
        # the old unconditional 'Family'.
        chart_title = 'Family' if (wings['spouses'] or wings['children']) else 'Ancestors'
        fan = self._markup(_render_fan_svg(ahnen, _FAN_GENERATIONS)) if len(ahnen) > 1 else None
        # Descendant explorer (#115): demoted from the home page (which now
        # carries the marriage-aware ancestor pedigree instead) to a
        # per-person opt-in link - the same `_build_tree_data`/fha-tree.js/
        # tree-adapter.js pipeline UNCHANGED, just re-seeded on THIS person
        # instead of the old apex-of-root_person. `_make_tree_ctx` already
        # returns None for a person with no descendant edges at all, so the
        # link/section simply does not render for a leaf of the tree - no
        # extra check needed here. `max_hops=_DESCENDANT_TREE_MAX_HOPS` is a
        # generous safety net, not a display bound (#152 review fix, P2): the
        # data stays complete (memoized `relationships` queries keep the walk
        # affordable - see that constant's own comment), and `initial_depth`
        # below is what actually bounds the client's initial paint.
        descendants_tree = self._make_tree_ctx(
            pid, 'descendants', _DESCENDANT_TREE_MAX_HOPS, page_dir,
            f'Descendants of {name}', initial_depth=4)

        ctx = {
            'display_id': fmt_id_display(pid), 'name': name,
            'alt_names': alt_names, 'tags': tags,
            'portrait': self._profile_photo_href(pid, page_dir),
            'family_strip': self._person_family_strip(pid, page_dir),
            'pedigree': pedigree,
            'chart_title': chart_title,
            'fan': fan,
            'descendants_tree': descendants_tree,
            'summary': summary,
            'biography_html': self._markup(biography_html) if biography_html else None,
            'stories_html': self._markup(stories_html) if stories_html else None,
            'research_html': self._markup(research_html) if research_html else None,
            # Workbench-only per-entry views of the same two append-logs:
            # one edit button per entry (empty lists in standalone builds).
            'stories_entries': [{'html': self._markup(e['html']), 'raw': e['raw']}
                                for e in stories_entries],
            'research_entries': [{'html': self._markup(e['html']), 'raw': e['raw']}
                                 for e in research_entries],
            # Linked-or-workbench (issue #117): open questions naming this
            # person, from notes/questions.md and every person's own research
            # file alike. Gated on `linked` (workbench always implies
            # linked - see the constructor), not narrowed to `workbench`
            # alone: a '## Q:' block still carries no `restricted:` field of
            # its own, and its `context:` can hold sensitive detail about a
            # living third party, but `--linked` is already this codebase's
            # trust boundary for real-but-unvetted content (same one
            # `drop_private=not self.linked` and `_person_is_redacted` rely
            # on) - an owner's own local `--linked` preview never leaves
            # their machine, so it is an acceptable home for this until a
            # privacy field lands. Narrower than that (`open_review_count`
            # below): that field is a workbench editing-queue affordance, not
            # unvetted research content, so it stays workbench-only.
            'open_questions': [{'heading': q['heading'], 'file': q['file'],
                                'html': self._markup(q['html'])}
                               for q in (self._person_open_questions(pid, page_dir)
                                         if self.linked else [])],
            'timeline': timeline, 'sources': sources, 'family': family, 'photos': photos,
            # Workbench-only fields (harmless in standalone - the template gates
            # every use on `workbench`): the record's on-disk relpath for the
            # "open file" button and living value for the "change..." affordance.
            # replace: keep the emitted path platform-independent (the index
            # stores the building OS's separators; committed fixtures must not
            # churn between machines).
            'record_relpath': str(row['path']).replace('\\', '/'),
            'living': (row['living'] or 'unknown'),
            'milestone_sources': self._person_milestone_sources(pid) if self.workbench else [],
            'biography_raw': biography_raw if self.workbench else '',
            # Wireframe wb-pointer: how many suggested claims naming this person
            # wait in the review queue (0 renders nothing).
            'open_review_count': (self.conn.execute(
                "SELECT COUNT(DISTINCT c.id) FROM claims c "
                "JOIN claim_persons cp ON c.id = cp.claim_id "
                "WHERE cp.person_id = ? AND c.status = 'suggested'", (pid,)
            ).fetchone()[0] if self.workbench else 0),
            'is_stub': (row['tier'] or '') == 'stub',
        }
        self._write_page(self.persons_dir / _page_filename(pid), 'person.html',
                         {'person': ctx, 'root_prefix': '..'})
        self._footnotes = None        # footnotes are strictly person-page-scoped

    def _person_header_meta(self, pid: str, display_name: str) -> tuple[list[str], list[str]]:
        """Alternate-name lines and editorial tag pills for the page header, read
        from the person `.md` front-matter (the index carries neither). Names come
        from `name_at_birth` (né/née), `married_name` (later), and the
        `also_known_as` / `name_variants` lists; tags from `tags`. Only non-redacted
        curated people get a page, so no living person's aliases surface; a
        `restricted` name variant (e.g. a deadname) is still dropped in standalone."""
        row = self.person_meta.get(pid)
        if not row:
            return [], []
        try:
            rec = read_record(self.archive_root / row['path'],
                               on_decode_error=_ignore_decode_error)
        except Exception as e:  # noqa: BLE001 - defensive; read_record does not
            # raise once on_decode_error is supplied, short of a genuine bug
            self.messages.append(
                f'WARNING: could not read {row["path"]} ({e}); this person\'s '
                f'alternate names and editorial tags are omitted from their page.')
            return [], []
        if rec.get('undecodable'):
            # This one speaks where its silent neighbours (`_provisional_vital`,
            # `_person_hypothesis_ties`, `_build_family_wings`) do not, and the
            # difference is what the message SAYS, not which file it names.
            # `_person_prose` reads this same file earlier in the same
            # `build_person_page` and warns about it - but its line ends
            # "skipping its prose", which a reader can fairly take as the whole
            # cost. The header is a second, visibly separate part of the page,
            # so a second line naming what else went missing is information,
            # not an echo. It costs one extra line per broken file per build
            # (this helper runs once per page), which is the noise the
            # per-vital and per-ancestor helpers had to avoid.
            self.messages.append(
                f'WARNING: could not read {row["path"]} (this file isn\'t saved '
                f'as UTF-8 text - a Windows editor\'s default encoding, often '
                f'cp1252, is the usual cause); this person\'s alternate names '
                f'and editorial tags are omitted from their page. Open it and '
                f'save it again choosing UTF-8, then run `fha site` again.')
            return [], []
        meta = rec['meta']
        restricted = set() if self.linked else self.restricted_names.get(pid, set())
        seen = {display_name.strip().lower()}
        alts: list[str] = []

        def norm(x) -> tuple[str, bool]:
            """(name, is_restricted). A variant may be a plain string or a
            `{value, restricted}` mapping - e.g. a deadname carrying `restricted`."""
            if isinstance(x, dict):
                v = x.get('value')
                r = str(x.get('restricted', '')).strip().lower() not in ('', 'false', 'none', '0')
                return (str(v).strip() if v else ''), r
            return (str(x).strip() if x else ''), False

        def add(label: str, value) -> None:
            v, item_restricted = norm(value)
            k = v.lower()
            if not v or k in seen:
                return
            if k in restricted or (item_restricted and not self.linked):
                return          # a restricted variant (deadname) never leaves a standalone build
            seen.add(k)
            alts.append(f'{label} {v}'.strip())

        add('né' if (row['sex'] or '').strip().lower() == 'm' else 'née', meta.get('name_at_birth'))
        add('later', meta.get('married_name'))
        for key in ('also_known_as', 'name_variants'):
            val = meta.get(key)
            for a in (val if isinstance(val, list) else ([val] if val else [])):
                add('', a)
        raw = meta.get('tags')
        tags = ([str(t).strip() for t in raw if str(t).strip()] if isinstance(raw, list)
                else [raw.strip()] if isinstance(raw, str) and raw.strip() else [])
        return alts, tags

    def _claim_is_own_vital(self, pid: str, claim_id: str, claim_type: str) -> bool:
        """Is this vital claim a record of THIS person, or of somebody else it
        also names?

        A birth certificate names the baby, both parents and the informant; a
        death certificate names the deceased, the widow and the child who
        reported it; a marriage licence names the couple and both sets of
        parents. Listing all of them in `persons:` is correct - `persons:` is
        the index of who a claim is about (SPEC §8.3) - so "which claims name
        this person" is the wrong question to ask when filling in his own
        Born/Died/Married. Asking it put a son's birth date in his mother's
        summary box and printed it under her name on the family chart (#126).

        `_lib.claim_is_own_vital` is the shared rule - the same one the GEDCOM
        writer, the WikiTree infoboxes and `fha views tree` read, so a person's
        page and their export cannot disagree about whose birthday it is. It
        says yes for a claim whose `roles:` map says nothing at all: the legacy
        claim, where the honest answer is that the archive does not know, and
        the honest rendering is the one this build has always produced.
        """
        return claim_is_own_vital(
            self.conn, pid, claim_id, claim_type, self._vital_subjects)

    def _person_summary(self, pid: str, page_dir: Path) -> list[dict]:
        """Accepted vital claims as the summary block (birth/death/marriage/…).

        Accepted-only in EVERY mode, deliberately - unlike the timeline, where
        the workbench also shows needs-review marked. The summary block is the
        person's settled headline facts; a parked vital belongs in the
        timeline (tagged) and the review surfaces, not the headline. First
        accepted claim per type wins, with `ORDER BY c.id` making that pick
        deterministic (row order otherwise varies by rowid and churns the
        committed example fixtures between rebuilds).

        Negated claims are excluded (COALESCE(c.negated, 0) = 0): a confirmed
        absence - a `--negated` birth minted as "not born in 1900" - is not a
        settled headline fact, and rendering it as `Born 1900` would assert the
        very thing the claim denies. Same posture as wikitree's spacetime and
        template exclusions.

        Claims of somebody ELSE's vital are excluded too (`_claim_is_own_vital`,
        #126). The SQL still gathers every accepted vital NAMING the person -
        it has to, since only the claim's whole `roles:` map can answer the
        question - and the filter runs over the result, before the
        first-of-each-type pick, so a relative's record cannot win the slot and
        thereby suppress the person's own.
        """
        living_filter = (
            '' if self.linked else
            "AND NOT EXISTS ("
            "  SELECT 1 FROM claim_persons cp2 JOIN persons p ON cp2.person_id = p.id "
            "  WHERE cp2.claim_id = c.id AND p.living IN ('true','unknown')"
            ")"
        )
        rows = self.conn.execute(
            "SELECT c.id, type, value, date_edtf, place_id, place_text, source_id, confidence "
            "FROM claims c "
            "JOIN claim_persons cp ON c.id = cp.claim_id "
            f"WHERE cp.person_id = ? AND c.status = 'accepted' "
            "AND COALESCE(c.negated, 0) = 0 "
            f"AND c.type IN ('birth','death','marriage','baptism','burial') {living_filter} "
            "ORDER BY c.id",
            (pid,),
        ).fetchall()
        rows = [r for r in rows if self._claim_is_own_vital(pid, r['id'], r['type'])]
        claim_persons: dict[str, str] = {}
        if self.workbench:
            # Raw person lists per claim, for the sourced-row edit affordance's
            # prefill (built here so the loop below needs no second query pass).
            for r in rows:
                prs = self.conn.execute(
                    'SELECT person_id FROM claim_persons WHERE claim_id = ? ORDER BY position',
                    (r['id'],)).fetchall()
                claim_persons[r['id']] = ','.join(fmt_id_display(p['person_id']) for p in prs)
        # Standalone: withold vitals whose only support is a withheld source; a fact
        # established exclusively by a restricted/DNA/publication_ok=false source must
        # not appear as a public datum with the citation silently redacted. A
        # restricted CLAIM is withheld too, regardless of its source.
        if not self.linked:
            # Show the vital even when its source is withheld (the citation is
            # redacted); withhold only a restricted claim or a hard-restricted
            # source (DNA / by-request / publication_ok:false). A vital tagging a
            # living person is already excluded by `living_filter` above.
            rows = [r for r in rows
                    if normalize_id(str(r['id'])) not in self.restricted_claims
                    and not self._source_hard_restricted(r['source_id'])]
        by_type: dict[str, sqlite3.Row] = {}
        for r in rows:
            by_type.setdefault(r['type'], r)   # first accepted of each type
        summary = []
        for t in _VITAL_ORDER:
            if t in by_type:
                r = by_type[t]
                row_out = {
                    'label': _VITAL_LABELS[t],
                    # #144 finding 4: a vital claim WITH a date_edtf shows that
                    # structured date as-is (base.html's own legend explains
                    # its bracket/tilde/question-mark notation - it is not
                    # prose and must not be translated to "before ..."
                    # phrasing). Only the free-text FALLBACK - a vital claim
                    # with no date_edtf at all - can carry the raw internal
                    # encoding a reader was never meant to see, so only that
                    # branch is scrubbed.
                    'value': r['date_edtf'] or _scrub_internal_encoding(r['value'] or ''),
                    'place': self._place_html(r['place_text'], r['place_id'], page_dir),
                    'source_html': self._markup(self._source_link(r['source_id'], page_dir)) if r['source_id'] else '',
                    'confidence': r['confidence'] or '',
                    'provisional': False,
                    'missing': False,
                }
                if self.workbench:
                    # The wireframe puts an edit affordance on EVERY summary
                    # row (owner complaint, live review 2026-07-16). A sourced
                    # vital's edit opens the claim editor prefilled with the
                    # claim's own data - editing the actual claim, not minting
                    # a duplicate (resolving the round-2 codex concern that
                    # removed the link: the modal now carries claim context).
                    row_out.update({
                        'claim_id': fmt_id_display(r['id']),
                        'claim_type': r['type'],
                        'value_raw': r['value'] or '',
                        'date_raw': r['date_edtf'] or '',
                        'place_text_raw': self._place_label(r['place_text'], r['place_id']),
                        'place_id_raw': fmt_id_display(r['place_id']) if r['place_id'] else '',
                        'persons_ids': claim_persons.get(r['id'], ''),
                    })
                summary.append(row_out)
        # Workbench only (owner decision 2026-07-10, plan 17 BUILD §2.2/§8.3): a
        # provisional birth/death - the unsourced `birth:`/`death:` frontmatter
        # estimate a human knows before the record exists - is surfaced marked
        # "estimate - unsourced", but ONLY for a vital that has no accepted claim
        # yet (a sourced claim supersedes the estimate everywhere). This never
        # runs in standalone or plain --linked: the published site stays
        # claims-only, so an unsourced estimate never leaves the machine.
        if self.workbench:
            # One source of truth (AGENTS_TOOLING.md symmetry rule): which vitals
            # get a provisional slot is `_lib.PROVISIONAL_VITAL_FIELDS`, not a
            # literal repeated here. Sorted for determinism - a frozenset's
            # iteration order is not guaranteed stable across runs.
            provisional_shown: set[str] = set()
            for t in sorted(PROVISIONAL_VITAL_FIELDS):
                if t in by_type:
                    continue   # a sourced claim wins - the estimate is superseded
                est = self._provisional_vital(pid, t)
                place = self._provisional_vital(pid, f'{t}_place') or ''
                # A place-only estimate (birth_place: with no birth:) is still
                # a row - the new set-estimate flags write either field alone.
                # Date and place stay SEPARATE fields all the way down: folding
                # them into one display string fed the edit modal an `mdate` of
                # "1923 - Kansas", which `person.estimate` rejects (codex round
                # 1). The template joins them for display and prefills
                # mdate/mplace independently.
                if est or place:
                    summary.append({
                        'label': _VITAL_LABELS[t],
                        'value': est or '',
                        'place': place,
                        'date_raw': est or '',
                        'place_raw': place,
                        'source_html': '',
                        'provisional': True,
                        'missing': False,
                    })
                    provisional_shown.add(t)
            # Wireframe (person.html "Died - not recorded / add"): a core vital
            # with neither a claim nor an estimate still gets a row, visibly
            # absent and one click from being filled. Only the big three - an
            # absent baptism/burial is normal, not a gap worth a row.
            for t in ('birth', 'marriage', 'death'):
                if t in by_type or t in provisional_shown:
                    continue
                summary.append({
                    'label': _VITAL_LABELS[t], 'value': '', 'place': '',
                    'source_html': '', 'provisional': False, 'missing': True,
                })
            # Keep the summary in the canonical vital order even after appending.
            order = {label: i for i, label in enumerate(
                _VITAL_LABELS[t] for t in _VITAL_ORDER)}
            summary.sort(key=lambda row: order.get(row['label'], 99))
        return summary

    def _provisional_vital(self, pid: str, field: str) -> str | None:
        """Read one provisional (unsourced) `birth:`/`death:` estimate from a
        person's frontmatter, or None. Non-load-bearing family knowledge
        (SPEC §9, `PROVISIONAL_VITAL_FIELDS`); the index does not carry it, so it
        is read from the record file on demand and only in workbench mode.

        This is asked up to four times per person page (birth/death x
        date/place - `PROVISIONAL_VITAL_FIELDS`), all against the SAME
        record. The parsed record is memoized in `_provisional_record_cache`
        after the first ask so the other three reuse it rather than each
        re-reading and re-parsing the file from disk (audit finding: this
        function's own comment used to note the fourfold repetition as an
        accepted cost rather than caching it - the same duplicate-expensive-
        call shape already fixed once elsewhere in this suite, report.py's
        `places.run_candidates()` case)."""
        row = self.person_meta.get(pid)
        if not row:
            return None
        if pid not in self._provisional_record_cache:
            try:
                rec = read_record(self.archive_root / row['path'],
                                   on_decode_error=_ignore_decode_error)
            except Exception:  # noqa: BLE001 - defensive; see _ignore_decode_error
                rec = None
            if rec is not None and rec.get('undecodable'):
                # Same file `_person_prose` reads for this same page - its own
                # WARNING already names it. Staying quiet here (rather than
                # repeating that warning for each of up to four field asks)
                # is the deliberate choice, not an oversight.
                rec = None
            self._provisional_record_cache[pid] = rec
        rec = self._provisional_record_cache[pid]
        if rec is None:
            return None
        val = rec['meta'].get(field)
        return str(val).strip() if val not in (None, '') else None

    def _person_prose(self, row: sqlite3.Row, page_dir: Path) -> tuple:
        """Biography, Stories and Research Notes HTML, read from the person `.md` body.

        Unaccepted `<!-- AI-DRAFT ... -->` prose is excluded before rendering
        (in both modes - the marker would render as escaped visible junk even
        in the linked preview): a draft is not yet content until `fha confirm
        draft` accepts it. A section that empties after the exclusion renders
        exactly like a person with no such section (the template's
        `{% if %}` guard skips the heading).

        A DAMAGED marker (usually a missing `-->`) means draft can no longer
        be told from accepted prose, so BOTH prose sections are withheld -
        the page renders as if no biography was written - and one warning
        names the file and the fix. The old behavior published the whole
        draft plus the dangling marker; withholding is the only safe
        rendering on a publication path, and the prose returns the moment
        the marker is repaired (or the draft accepted) and the site rebuilt."""
        try:
            rec = read_record(self.archive_root / row['path'],
                               on_decode_error=_raise_friendly_decode_error)
        except Exception as e:
            self.messages.append(f'WARNING: could not read {row["path"]} ({e}); skipping its prose.')
            return '', '', '', '', [], []
        render = lambda tok, disp=None: self.render_token(tok, page_dir, disp)  # noqa: E731 - tiny closure
        embed = lambda t, c: self._render_embed(t, c, page_dir)  # noqa: E731
        # Apply the `<!-- private -->` fence to the whole body BEFORE section
        # extraction. Otherwise an opener that sits above a `## Research Notes`
        # heading is dropped with its parent section, and the extracted body
        # sees only the trailing `<!-- /private -->` - leaving the private
        # text unfenced and publishable on a standalone build.
        body = rec['body']
        stories = rec['stories']
        # The Biography section text exactly AS WRITTEN - private-fence
        # markers and any AI-DRAFT/AI-ACCEPTED markers intact - captured
        # from the UNTOUCHED body before ANY publish-time processing below,
        # for the workbench editor prefill (`person.edit --section
        # biography`'s whole-section REPLACE target). `apply_private_fence`
        # is safe to run AFTER this in workbench mode (`dp` is always False
        # here, since workbench requires linked=True) - it only strips the
        # marker COMMENTS in that mode, never any text - but capturing
        # BEFORE it anyway means the editor prefill still shows a real
        # `<!-- private -->` fence if one is present, not laundered-away
        # plain text a later small edit could re-publish on a standalone
        # build (P2 codex finding, round 7, PR #30 - the round-5 fix here
        # already protected a pending AI-DRAFT the same way).
        bio_as_written = (_extract_section(body, 'Biography') or '').strip()
        # Research Notes' pre-fence content, captured for the SAME reason and
        # at the SAME point as bio_as_written above, plus one more: the
        # unfilled-placeholder check just below (person_section_is_unfilled)
        # must run before apply_private_fence touches the body, because the
        # Research Notes placeholder embeds a `<!-- private -->` example
        # block - checking post-fence text would never match it in either
        # build mode, once that block has been dropped (standalone) or
        # unwrapped (linked). The workbench per-entry edit rows further down
        # reuse this same capture rather than re-reading the section: two
        # reads of one string is two things to keep in step, and the later
        # one sat AFTER `apply_private_fence` had already run on `body`,
        # which is exactly the ordering hazard this capture exists to avoid.
        research_as_written = (_extract_section(body, 'Research Notes') or '').strip()
        dp = not self.linked
        if body:
            body = apply_private_fence(body, drop=dp)
        if stories:
            stories = apply_private_fence(stories, drop=dp)
        # A section that holds NOTHING but its own scaffold placeholder text
        # was never actually filled in by a human - render it exactly like an
        # empty section, not like real content (#125). Without this, a
        # freshly-scaffolded person's Biography/Research Notes published the
        # archive owner's own authoring instructions verbatim, as if they
        # were the person's real story - the "unfilled" case `_extract_section`
        # already treats an empty/`*(none yet)*` section as, extended to the
        # OTHER placeholder wording the scaffold writes (see
        # person_section_is_unfilled for why this cannot be a substring/fuzzy
        # test: it must exact-match so real content sharing a few of the
        # scaffold's words still publishes).
        bio = (None if person_section_is_unfilled('Biography', bio_as_written)
               else _extract_section(body, 'Biography'))
        research = (None if person_section_is_unfilled('Research Notes', research_as_written)
                    else _extract_section(body, 'Research Notes'))
        problem: str | None = None
        if bio:
            bio, problem = strip_unaccepted_drafts(bio)
            bio = bio.strip()
        if stories and problem is None:
            stories, problem = strip_unaccepted_drafts(stories)
            stories = stories.strip()
        if research and problem is None:
            research, problem = strip_unaccepted_drafts(research)
            research = research.strip()
        if problem is not None:
            self.messages.append(
                f'WARNING: a draft marker in {row["path"]} is damaged ({problem}) - '
                'fix the marker or remove the draft, then rebuild. Until then this '
                "person's Biography, Stories and Research Notes are withheld from the site."
            )
            return '', '', '', '', [], []
        # Private fences were already applied to the whole body above, so
        # _prose_to_html need not re-apply them here.
        biography_html = _prose_to_html(bio, render, embed, drop_private=dp) if bio else ''
        stories_html = _prose_to_html(stories, render, embed, drop_private=dp) if stories else ''
        research_html = _prose_to_html(research, render, embed, drop_private=dp) if research else ''
        # Workbench-only: the same Stories/Research text again, split into
        # its append-log entries so each can carry its own edit button
        # (owner request, review 2026-07-16). The DISPLAY list still comes
        # from the filtered text above (a button only ever names an entry the
        # page actually shows), but each entry's `raw` - the modal's
        # old_text/replacement seed - is matched back to the entry AS WRITTEN
        # on disk: `person.run_edit_note` compares exact paragraphs, so a
        # `<!-- private -->` fence or `<!-- AI-ACCEPTED -->` marker stripped
        # for display would make every edit refuse "entry not found" (and
        # seed a replacement with the markers laundered away - P2 codex
        # finding, round 2, PR #31).
        stories_entries: list[dict] = []
        research_entries: list[dict] = []
        if self.workbench:
            stories_as_written = (rec['stories'] or '')
            stories_entries = self._log_entries_with_raw(
                stories or '', stories_as_written, render, embed, dp)
            research_entries = self._log_entries_with_raw(
                research or '', research_as_written, render, embed, dp)
        # `bio_as_written` (NOT the fence-processed, draft-stripped `bio`
        # used for the render above) is returned alongside the rendered
        # HTML: it is the exact text `person.edit --section biography`
        # would overwrite, private-fence and AI-DRAFT/AI-ACCEPTED markers
        # intact, so the workbench's whole-section REPLACE editor can never
        # silently launder away any of them on a human's small edit.
        return (biography_html, stories_html, research_html, bio_as_written,
                stories_entries, research_entries)

    def _log_entries_with_raw(self, display_section: str, as_written_section: str,
                              render, embed, dp: bool) -> list[dict]:
        """Workbench per-entry edit rows for one Stories/Research append-log.

        Each as-written entry is put through the SAME display filters the
        whole section got (fence markers stripped in workbench mode,
        AI-ACCEPTED markers removed, unaccepted drafts dropped) and used as a
        lookup key, so every shown entry can carry its exact on-disk text as
        `raw`. An entry the filters would hide never becomes a key (it is not
        shown, so no button names it); a display entry with no match falls
        back to its display text, where the edit engine's exact-match rule
        still refuses plainly rather than mis-editing."""
        lookup: dict[str, str] = {}
        for w in split_log_entries(as_written_section or ''):
            filtered = apply_private_fence(w, drop=False)
            filtered, problem = strip_unaccepted_drafts(filtered)
            key = filtered.strip()
            if problem is None and key and key not in lookup:
                lookup[key] = w
        return [
            {'html': _prose_to_html(e, render, embed, drop_private=dp),
             'raw': lookup.get(e.strip(), e)}
            for e in split_log_entries(display_section or '')]

    def _person_open_questions(self, pid: str, page_dir: Path) -> list[dict]:
        """This person's open questions (issue #117), rendered through the
        same prose-to-HTML pipeline Research Notes uses, so a `[[P-…]]`
        cross-link and any `<!-- private -->` fence in a question's
        `context:` behave exactly as they do everywhere else on the page.

        Reads `self.person_questions`, built once for the whole build by
        `_load_open_questions` - never called here, so a person with no
        indexed questions costs one dict lookup, not a re-scan of the log.
        Only called from build_person_page when self.linked (see there for
        why - workbench always implies linked, so this covers both); `dp`
        reads `not self.linked` for the same reason every other unredacted
        surface on this page does.
        """
        render = lambda tok, disp=None: self.render_token(tok, page_dir, disp)  # noqa: E731
        embed = lambda t, c: self._render_embed(t, c, page_dir)  # noqa: E731
        dp = not self.linked
        out: list[dict] = []
        for info in self.person_questions.get(pid, []):
            body = _question_block_body(info['block'])
            html = _prose_to_html(body, render, embed, drop_private=dp) if body.strip() else ''
            if html:
                out.append({'heading': info['heading'], 'file': info['file'], 'html': html})
        return out

    def _person_timeline(self, pid: str, page_dir: Path) -> list[dict]:
        """The person's claims grouped by decade (TOOLING §12; same shape as
        `fha views timeline`'s main chronology). Statuses diverge by audience
        (owner decision 2026-07-22): the published standalone timeline shows
        accepted claims only (with a low-confidence indicator - SPEC §8.5's
        "sometimes that's the best we ever get" facts publish, flagged);
        linked/workbench also shows needs-review claims, clearly marked as
        unconfirmed. Suggested claims never render here in any mode. (`fha
        views timeline`, the private research artifact, keeps needs-review
        with the same wording - the divergence is public-vs-working surface,
        not two rules.)

        Three rendering fixes live here (#127, #128, #140; #129's fix is
        template-only, see person.html):
          - #127: each entry carries `place_redundant` - true when the claim's
            own value text already names the place naturally ("moved to
            Millbrook to farm"), decided by `_place_mention_span` (whole
            words, loose punctuation, and - #127 reopened - a match on just
            the place label's LEADING component counts too, so "resided at
            the family home" does not get a redundant trailing "at the
            family home, Cook County, Illinois"; see that function's
            docstring for the full reasoning). The template only appends a
            trailing place mention when this is false, so a place already
            stated in the sentence is never doubled up as a bare "@ Place"
            tag (or, post-#127-reopened, as a same-named "at Place" repeat
            either). When it IS stated in the sentence, `_timeline_value_html`
            moves the place-page link onto those words, so suppressing the
            repeat never costs the reader the link when the place is
            registered and linkable. Most one-off residence/travel claims
            never get a place registered at all (SPEC.md), so `place_id in
            self.place_pages` also gates a SECOND field, `place_remainder`
            (#127 reopened, finding 1 follow-up): for an unlinkable claim,
            `_place_trailing_remainder` returns the label's own droppable
            qualifier (e.g. ", Dutchess County, New York") instead of
            nothing, and the template prints it as a plain continuation of
            the sentence rather than either a full duplicate "at Placename"
            or - the prior commit's own behavior - silently dropping the
            qualifier with no page to recover it from. A coordinated
            compound ("Trinidad and Tobago") has no such remainder and
            keeps the prior commit's all-or-nothing behavior regardless of
            `place_id` - see `_place_trailing_remainder`'s docstring.
          - #128: the SQL sorts rows by `date_min` (a widened, sortable value)
            but decade grouping reads `date_edtf` via `_decade_header` - a
            DIFFERENT field, deliberately (see that function's docstring): an
            approximate '1840~' widens date_min to '1839-01-01' and would
            group into the wrong decade if grouping used date_min too. Those
            two fields can disagree for an uncertain/ranged date, so a
            straight linear pass over date_min order can split one decade's
            entries into two non-contiguous groups with another decade's
            heading between them. The fix sorts a COPY of the rows by decade
            before grouping; Python's sort is stable, so date_min order
            survives as the within-decade tiebreak.
          - #140: a claim's value text can carry internal-only encoding a
            reader was never meant to see - a bare `(C-xxxxxxxxxx)` claim-id
            parenthetical, or a raw `[..YYYY]` "before" date bracket - so
            `_scrub_internal_encoding` runs on it before `_place_mention_span`
            (see below) and before `_timeline_value_html` escapes it for
            display.
        """
        status_filter = ("c.status IN ('accepted','needs-review')" if self.linked
                         else "c.status = 'accepted'")
        living_filter = (
            '' if self.linked else
            "AND NOT EXISTS ("
            "  SELECT 1 FROM claim_persons cp2 JOIN persons p ON cp2.person_id = p.id "
            "  WHERE cp2.claim_id = c.id AND p.living IN ('true','unknown')"
            ")"
        )
        rows = self.conn.execute(
            "SELECT DISTINCT c.id, c.date_edtf, c.date_min, c.type, c.value, c.place_id, c.place_text, c.source_id, "
            "c.status, c.confidence, c.reviewed "
            "FROM claim_persons cp JOIN claims c ON cp.claim_id = c.id "
            f"WHERE cp.person_id = ? AND {status_filter} {living_filter} "
            "ORDER BY CASE WHEN c.date_min IS NULL OR c.date_min = '' THEN 1 ELSE 0 END, c.date_min ASC",
            (pid,),
        ).fetchall()
        # Standalone: show the event with its citation redacted when the source is
        # merely withheld (names a living person); omit only a restricted claim or a
        # hard-restricted source. Events tagging a living person are already excluded
        # by `living_filter`.
        if not self.linked:
            rows = [r for r in rows
                    if normalize_id(str(r['id'])) not in self.restricted_claims
                    and not self._source_hard_restricted(r['source_id'])]
        def _decade_sort_key(decade: str | None) -> tuple[int, int]:
            if decade is None:
                return (1, 0)   # undated sorts after every dated decade
            return (0, int(decade[:-1]))   # '1930s' -> 1930

        rows_with_decade = [(r, _decade_header(r['date_edtf'])) for r in rows]
        rows_with_decade.sort(key=lambda item: _decade_sort_key(item[1]))

        groups: list[dict] = []
        current: str | None = '\x00'   # sentinel distinct from None (undated)
        entries: list[dict] = []
        for r, decade in rows_with_decade:
            if decade != current:
                if entries:
                    groups.append({'decade': None if current == '\x00' else current, 'entries': entries})
                current = decade
                entries = []
            # Scrubbed the same as value_text below (adversarial review,
            # round 4 audit): place_label feeds `_place_trailing_remainder`,
            # whose OWN output prints straight onto the page as a sentence
            # continuation (person.html: `{{ e.place_remainder }}`) with no
            # further scrub of its own - an unscrubbed place_text carrying a
            # bare citation id or date bracket reached the reader unchanged.
            place_label = _scrub_internal_encoding(self._place_label(r['place_text'], r['place_id']))
            # #140: scrub BEFORE the place-mention span is computed, so a
            # length change from stripping a claim-id parenthetical or
            # expanding a `[..YYYY]` date never desyncs `mention`'s indices
            # from the text `_timeline_value_html` actually renders.
            value_text = _scrub_internal_encoding(r['value'] or '')
            mention = _place_mention_span(value_text, place_label)
            place_linkable = bool(r['place_id']) and r['place_id'] in self.place_pages
            entries.append({
                'date': r['date_edtf'] or '(undated)', 'type': r['type'],
                'value': self._timeline_value_html(value_text, mention, r['place_id'], page_dir),
                'place': self._place_html(r['place_text'], r['place_id'], page_dir),
                'place_redundant': mention is not None,
                'place_remainder': _place_trailing_remainder(value_text, place_label, mention, place_linkable),
                'source_html': self._markup(self._source_link(r['source_id'], page_dir)) if r['source_id'] else '',
                'status': r['status'], 'confidence': r['confidence'] or '',
                'parked': r['reviewed'] or '',
            })
        if entries:
            groups.append({'decade': None if current == '\x00' else current, 'entries': entries})
        return groups

    def _person_sources(self, pid: str, page_dir: Path) -> list[dict]:
        """The person's Sources as a numbered footnote list - each source's human
        name linked to its page, keyed to the superscript numbers used inline.

        Called after the summary/timeline (which already numbered the sources they
        cite, in reading order); any remaining sources that cite the person but are
        not referenced inline are appended so the list stays complete. The same
        two-table UNION as `fha views sources-index`."""
        status_filter = '' if self.linked else "AND c.status = 'accepted'"
        rows = self.conn.execute(
            f'SELECT DISTINCT c.source_id FROM claim_persons cp JOIN claims c ON cp.claim_id = c.id '
            f'WHERE cp.person_id = ? {status_filter} '
            'UNION SELECT DISTINCT source_id FROM source_people WHERE person_id = ?',
            (pid, pid),
        ).fetchall()
        for r in rows:
            sid = normalize_id(str(r[0])) if r[0] else None
            if not sid or sid not in self.source_meta:
                continue
            if not self.linked and sid not in self.source_pages:
                continue
            self._footnote_number(sid)          # ensure every person-source is numbered
        out: list[dict] = []
        for sid in (self._footnote_seq or []):
            if sid == _RESTRICTED_FN:      # the single shared "restricted source" entry
                out.append({'num': self._footnotes[sid],
                            'html': self._markup(f'<span class="redacted">{_RESTRICTED_LABEL}</span>')})
                continue
            row = self.source_meta.get(sid)
            if row is None:
                continue
            title = _escape(row['title'] or fmt_id_display(sid))
            if sid in self.source_pages:
                href = html.escape(_rel_href(self.sources_dir / _page_filename(sid), page_dir), quote=True)
                title = f'<a href="{href}">{title}</a>'
            out.append({'num': self._footnotes[sid], 'html': self._markup(title)})
        return out

    def _person_milestone_sources(self, pid: str) -> list[dict]:
        """id/title pairs for the workbench milestone modal's Source picker -
        every source that already cites this person, so 'Add a milestone' can
        point at real evidence instead of the person composing a raw S-id from
        memory. Workbench mode always runs --linked (redaction is moot: the
        combination workbench+standalone is refused in run_site), so this skips
        the footnote numbering and redacted-source placeholder `_person_sources`
        needs for the public page and just lists id + title, sorted by title."""
        rows = self.conn.execute(
            'SELECT DISTINCT c.source_id FROM claim_persons cp JOIN claims c ON cp.claim_id = c.id '
            'WHERE cp.person_id = ? '
            'UNION SELECT DISTINCT source_id FROM source_people WHERE person_id = ?',
            (pid, pid),
        ).fetchall()
        out: list[dict] = []
        seen: set[str] = set()
        for r in rows:
            sid = normalize_id(str(r[0])) if r[0] else None
            if not sid or sid in seen or sid not in self.source_meta:
                continue
            seen.add(sid)
            title = self.source_meta[sid]['title'] or fmt_id_display(sid)
            out.append({'id': fmt_id_display(sid), 'title': title})
        out.sort(key=lambda e: e['title'].lower())
        return out

    def _has_public_claim(self, pid1: str, pid2: str) -> bool:
        """Return True if the relationship between two persons may be shown.

        A relationship is suppressed only when its every backing claim is a
        restricted claim or is sourced *exclusively* from a hard-restricted source
        (restricted / DNA / by-request / publication_ok:false). A relationship
        evidenced only by a source withheld because it names a living person is
        still shown - the living person is redacted elsewhere, but the deceased
        pair's relationship is not (only living people are redacted outright).

        Standalone, only an ACCEPTED claim can vouch for a tie (owner decision
        2026-07-22: the public site publishes settled facts only) - a pair whose
        every backing claim is still needs-review is withheld until one is
        accepted. The query still fetches needs-review rows so the no-claims
        fallback below stays correct: a tie with parked evidence is "claimed
        but unsettled" (hidden), NOT "no claims at all" (a YAML-only belief,
        which renders as such)."""
        rows = self.conn.execute(
            "SELECT c.id, c.source_id, c.status FROM claims c "
            "JOIN claim_persons cp1 ON c.id = cp1.claim_id AND cp1.person_id = ? "
            "JOIN claim_persons cp2 ON c.id = cp2.claim_id AND cp2.person_id = ? "
            "WHERE c.status IN ('accepted','needs-review')",
            (pid1, pid2),
        ).fetchall()
        for r in rows:
            if self._claim_row_is_publishable(r['id'], r['source_id'], r['status']):
                return True
        return not rows  # no claims at all → relationship came from YAML directly, show it

    def _claim_row_is_publishable(self, claim_id, source_id, status) -> bool:
        """Return True if ONE specific claim (already known to name whoever the
        caller cares about) may be shown - the per-claim rule `_has_public_claim`
        applies to every row of a pair's claims before OR-ing them together.
        Exposed separately so a caller that must know whether a SPECIFIC edge is
        public - not just "is there SOME public claim connecting these two
        people, about anything at all" - can ask the narrower, correct question
        (site.py Codex review, PR #152 round, P1: `_has_public_claim`'s pair-wide
        OR let a restricted parent-child tie read as public whenever the same two
        people also shared an unrelated public claim, e.g. a census entry).

        A `relationships` row whose `claim_id` names no row that actually
        exists in `claims` (`status is None` after the LEFT JOIN - a bare
        edge with no backing claim at all, e.g. a hand-written
        `relationships:` entry) is publishable: the same "nothing to hide
        behind" rule `_has_public_claim` applies via its own `not rows`
        fallback for a pair with zero claims connecting them. A real claim
        row always carries a non-null status, so `status is None` is the
        correct "no such claim" signal - not `claim_id is None`, since a
        dangling/placeholder claim_id (present but naming nothing real) must
        read the same way."""
        if status is None:
            return True
        if not self.linked and status != 'accepted':
            return False
        if normalize_id(str(claim_id)) in self.restricted_claims:
            return False
        return not self._source_hard_restricted(source_id)

    def _has_public_parent_edge(self, child_pid: str, parent_pid: str) -> bool:
        """Return True if the SPECIFIC parent-child relationship edge - not just
        any claim naming both people, about anything - has at least one public
        backing claim.

        `_has_public_claim` answers a broader question (is there ANY publishable
        claim connecting these two people) that a completely unrelated public
        claim - a shared census entry, a residence record - can satisfy even
        when the parent-child tie itself is evidenced only by a restricted
        claim. A parent-slot / sibling-eligibility check needs the narrower
        answer: is THIS relationship - the one about to be shown - itself
        backed by something public (site.py Codex review, PR #152 round, P1)."""
        rows = self.conn.execute(
            "SELECT r.claim_id, c.source_id, c.status FROM relationships r "
            "LEFT JOIN claims c ON r.claim_id = c.id "
            "WHERE r.person_id = ? AND r.rel = 'parent' AND r.other_id = ?",
            (child_pid, parent_pid),
        ).fetchall()
        return any(
            self._claim_row_is_publishable(r['claim_id'], r['source_id'], r['status'])
            for r in rows)

    def _is_living_tagged_photo(self, alias_path: str) -> bool:
        """Return True when any person tagged to this photo in the catalog is living/unknown.

        Source-page image derivatives must skip photos co-tagged to living persons
        even when the source itself is otherwise public - the same rule applied
        to person photo strips applies here.

        Both spellings of the path are checked. When reconcile has flagged a
        photo missing, its tags move to the 'MISSING:' key while `source_files`
        still names the plain path - so a photo that comes back (a reconnected
        drive) before the next scan would otherwise look untagged and publish
        the living person the gate exists to protect.

        THIS GATE FAILS OPEN. With no catalog to ask - it was never built, it is
        out of date, it will not open - the answer is False and the photo
        publishes. That is the owner's decision (2026-08-16): the site is meant
        to build without `.cache/photos.sqlite`, and refusing every source image
        until someone runs `fha photoindex` costs more than it saves. What the
        build owes the researcher instead is to say so, once, in words - which
        is `_warn_living_photo_check_unavailable`'s whole job. Watching for that
        warning is how this stays safe."""
        if self.photos_conn is None:
            return False
        try:
            rows = self.photos_conn.execute(
                'SELECT person_ref FROM photo_people WHERE path = ? OR path = ?',
                (alias_path, f'{_MISSING_PREFIX}{_live_alias(alias_path)}'),
            ).fetchall()
        except sqlite3.DatabaseError:
            # The catalog opened and then failed a query: from here on it can
            # answer nothing, so the build's warning must name it as unreadable
            # rather than as whatever it looked like at open time.
            self.photos_status = 'unreadable'
            return False
        for row in rows:
            person = self.person_meta.get(row['person_ref'] or '')
            # A deceased `restricted: by-request` person is redacted too, not just
            # living/unknown - mirror the person photo strips, which gate on the
            # full _person_is_redacted predicate.
            if person and self._person_is_redacted(person):
                return True
        return False

    def _warn_living_photo_check_unavailable(self) -> None:
        """Say once that this build published photos nobody checked for living-person tags.

        Called when a standalone build has just put a real image on a page while
        `_is_living_tagged_photo` had no catalog to consult. The check is the
        only thing standing between a photo tagged to a living person and a
        snapshot meant to be handed round the family, and it failed open - so
        the researcher is the check now, and a person cannot watch for something
        nobody told him about.

        Once per build, not once per photo: a site with two hundred scans would
        bury the one sentence that matters under two hundred copies of it, and a
        warning nobody finishes reading is a warning nobody read. It carries the
        cause and the two ways out - rebuild the catalog, or look through the
        images yourself - because a message that names no fix is a dead end.
        """
        if self._living_photo_warning_sent:
            return
        self._living_photo_warning_sent = True
        reason = _PHOTO_CATALOG_TROUBLE.get(
            self.photos_status, 'the photo catalog is not available')
        self.messages.append(
            f'WARNING: {reason}, so the photos published in this site were NOT '
            'checked against it for tags naming living people - a photograph '
            'of someone still living may be in it. Run `fha photoindex`, then '
            '`fha site` again, to have that check applied; until then, look '
            'through the images on the source pages yourself before you share '
            'this site.'
        )

    def _person_family(self, pid: str, page_dir: Path) -> list[dict]:
        """Friends & Family from the relationships edges, grouped by relation."""
        rows = self.conn.execute(
            'SELECT DISTINCT rel, other_id FROM relationships WHERE person_id = ?', (pid,)
        ).fetchall()
        by_rel: dict[str, list[str]] = {}
        for r in rows:
            # Standalone: omit the relationship entirely rather than showing a "Living
            # Person" placeholder - the existence and type of a family link is itself
            # personal information that should not be published.
            if not self.linked:
                meta = self.person_meta.get(r['other_id'])
                if meta and self._person_is_redacted(meta):
                    continue
                # Omit relationships whose only evidence is from withheld sources
                # (restricted, DNA, publication_ok=false, or living-linked).
                if not self._has_public_claim(pid, r['other_id']):
                    continue
            by_rel.setdefault(r['rel'], []).append(self._person_link(r['other_id'], page_dir))
        groups = []
        for rel, label in _FAMILY_GROUPS:
            if rel in by_rel:
                groups.append({'label': label, 'members': [self._markup(m) for m in sorted(by_rel[rel])]})
        return groups

    def _person_family_strip(self, pid: str, page_dir: Path) -> dict | None:
        """A compact parents / spouses / siblings / children map for the head of
        a person page - one hop up, sideways, and down, plus siblings, and
        nothing deeper. Redaction + public-claim gates match Friends & Family
        (and the same gate `_build_family_wings` applies to the pedigree's
        spouse/child columns); siblings are reached only through a public,
        non-redacted parent."""
        def edge(person: str, rel: str) -> list[str]:
            return [r['other_id'] for r in self.conn.execute(
                'SELECT DISTINCT other_id FROM relationships WHERE person_id = ? AND rel = ?',
                (person, rel))]

        def links(ids, evidence_with):
            out, seen = [], set()
            for oid, ev in ids:
                if oid == pid or oid in seen:
                    continue
                seen.add(oid)
                meta = self.person_meta.get(oid)
                if not self.linked:
                    # A stub (no meta row) has no page and no known living
                    # status; skip rather than emit a raw P-id chip.
                    if meta is None:
                        continue
                    if self._person_is_redacted(meta):
                        continue
                    if not self._has_public_claim(ev, oid):
                        continue
                out.append(self._markup(self._person_link(oid, page_dir)))
            return out

        parent_ids = edge(pid, 'parent')
        child_ids = edge(pid, 'child')
        spouse_ids = edge(pid, 'spouse')
        parents = links([(p, pid) for p in parent_ids], None)
        children = links([(c, pid) for c in child_ids], None)
        spouses = links([(s, pid) for s in spouse_ids], None)

        sib_pairs, sib_seen = [], set()
        for par in parent_ids:
            pm = self.person_meta.get(par)
            if not self.linked and ((pm and self._person_is_redacted(pm))
                                    or not self._has_public_claim(pid, par)):
                continue
            for k in edge(par, 'child'):
                if k != pid and k not in sib_seen:
                    sib_seen.add(k)
                    sib_pairs.append((k, par))     # evidence is the shared parent
        siblings = links(sib_pairs, None)

        groups = {'parents': parents, 'spouses': spouses, 'siblings': siblings, 'children': children}
        if self.workbench:
            # `person.relate`'s whole output for an unsourced tie is a
            # `relationships:` hypothesis entry, never an accepted claim, so
            # it never reaches the `relationships` index table the groups
            # above are built from - the "+ add" button's own write would
            # otherwise be invisible on the very page it was added from (P2
            # codex finding, round 7, PR #30). Workbench-only: merged in
            # after the accepted groups, never counted for `not self.linked`
            # standalone/redaction purposes (this whole branch never runs there).
            # `claim_backed` lets the merge skip a hypothesis whose tie an
            # accepted claim already draws - the normal "+ add now, source it
            # in review later" lifecycle leaves the hypothesis entry in the
            # record until lint walks the human through linking its claim, and
            # without the skip the strip would show that person twice.
            claim_backed = {'parents': set(parent_ids),
                            'spouses': set(spouse_ids),
                            'siblings': sib_seen,
                            'children': set(child_ids)}
            hyp_groups = self._person_hypothesis_ties(pid, page_dir,
                                                      skip=claim_backed)
            for key, hyp_links in hyp_groups.items():
                groups[key] = groups[key] + hyp_links
        if not any(groups.values()):
            return None
        return groups

    def _person_hypothesis_ties(self, pid: str, page_dir: Path,
                                skip: dict[str, set] | None = None) -> dict[str, list[str]]:
        """Workbench-only companion to `_person_family_strip`: this person's
        OWN `relationships:` entries with `status: hypothesis` (SPEC §9 -
        the whole output of an unsourced `person.relate` / the family
        strip's "+ add" button), read straight from the record file since
        a hypothesis is never indexed - the `relationships` table only ever
        carries accepted-claim-backed edges (`fha xref`'s "typed
        relationship graph" reads only genetic/social edges the same way).
        Grouped by the entry's own `type` into the same four keys
        `_person_family_strip` uses, so its caller can merge them straight
        in; each link is tagged "(hypothesis)" so it is never mistaken for
        a sourced tie.

        `skip` maps each group key to the P-ids the caller already shows
        from claim-backed edges: a hypothesis for a tie a claim already
        draws is suppressed rather than shown as a duplicate row - the
        state a normal "+ add first, source it later" lifecycle passes
        through until lint walks the human through linking the claim."""
        row = self.person_meta.get(pid)
        if row is None:
            return {}
        try:
            rec = read_record(self.archive_root / row['path'],
                               on_decode_error=_ignore_decode_error)
        except Exception:  # noqa: BLE001 - an unreadable record just contributes nothing here
            return {}
        if rec.get('undecodable'):
            # Same file `_person_prose` already reads for this page and
            # warns about; contributing nothing here is deliberate, not an
            # oversight - see `_ignore_decode_error`.
            return {}
        meta = rec['meta']
        out: dict[str, list[str]] = {}
        for group, target in self._hypothesis_tie_ids_from_meta(meta, pid):
            if skip is not None and target in skip.get(group, ()):
                continue
            # Build the whole raw HTML string first, then wrap it ONCE -
            # `_person_link` returns a plain (already-escaped-at-the-leaves)
            # string, and concatenating a `Markup`-wrapped fragment with a
            # plain string via `+` would re-escape the plain side, turning
            # this span's own tags into literal text.
            link_html = self._person_link(target, page_dir) + ' <span class="wb-hypothesis-tag">(hypothesis)</span>'
            out.setdefault(group, []).append(self._markup(link_html))
        return out

    @staticmethod
    def _hypothesis_tie_ids_from_meta(meta: dict, pid: str) -> list[tuple[str, str]]:
        """(group, target P-id) pairs for a record's `relationships:` entries
        with `status: hypothesis` - the shared id-level core of
        `_person_hypothesis_ties` (family strip) and the pedigree's
        slot-occupancy check. Groups are the family-strip keys
        ('parents'/'spouses'/'siblings'/'children')."""
        group_of_type = {'parent': 'parents', 'spouse': 'spouses',
                         'sibling': 'siblings', 'child': 'children'}
        out: list[tuple[str, str]] = []
        for entry in (meta.get('relationships') or []):
            if not isinstance(entry, dict):
                continue
            if str(entry.get('status') or '').strip().lower() != 'hypothesis':
                continue
            group = group_of_type.get(str(entry.get('type') or '').strip().lower())
            if group is None:
                continue
            target_ids = extract_bare_ids(str(entry.get('to') or ''))
            if not target_ids:
                continue
            target = normalize_id(target_ids[0])
            if target == pid:
                continue
            out.append((group, target))
        return out

    def _hypothesis_parent_ids(self, pid: str) -> list[str]:
        """Workbench-only: parent P-ids this person records as frontmatter
        `relationships:` hypotheses (never indexed - the `relationships`
        table carries only accepted-claim-backed edges). The pedigree's
        slot-occupancy check counts these so a parent just added through
        the add-family flow fills their slot instead of leaving an
        'Unknown - add' card that would mint a duplicate stub (P2 codex
        finding, round 3, PR #31)."""
        row = self.person_meta.get(pid)
        if row is None:
            return []
        try:
            rec = read_record(self.archive_root / row['path'],
                               on_decode_error=_ignore_decode_error)
        except Exception:  # noqa: BLE001 - an unreadable record just contributes nothing here
            return []
        if rec.get('undecodable'):
            # Contributing nothing here is deliberate, not an oversight - see
            # `_ignore_decode_error`. Kept symmetric with the sibling read in
            # `_person_hypothesis_ties`, which stays quiet for the same reason
            # (see its comment), and staying quiet costs no coverage even
            # though this one is reached for ancestors rather than only for
            # the page's own subject. This method reads the record of whoever
            # the pedigree walk is standing on, and it only runs at all under
            # `if self.workbench:` - a mode that builds a page for EVERY
            # person in the index, stubs included (see `prepare()`'s
            # person_pages loop). So whoever's file this is has a page of
            # their own in this same build, and `_person_prose` names the file
            # there. `prepare()`'s file-scan warning is indeed standalone-only
            # and never runs here, but it is not the only channel: the page
            # build is. A warning here would just repeat that line once per
            # descendant whose pedigree walks through this ancestor.
            return []
        meta = rec['meta']
        out: list[str] = []
        for group, target in self._hypothesis_tie_ids_from_meta(meta, pid):
            if group == 'parents' and target not in out:
                out.append(target)
        return out

    def _person_photos(self, pid: str, page_dir: Path) -> list[dict]:
        """Photo strip from `.cache/photos.sqlite` (`photo_people`), one entry
        per variation group. Omitted silently when the photo index is absent or
        stale (`self.photos_conn` None) - it is an optional enrichment, never a
        build blocker. Uses the connection opened once in `prepare()`.

        Rows reconcile has flagged 'MISSING:' are dropped at the query, not at
        render time: the file is gone, so it could only ever produce a broken
        picture - and, worse, a vanished row still carries `is_primary`, so
        leaving it in would let it win the one-entry-per-group pick below and
        take the still-present back scan off the page with it."""
        if self.photos_conn is None:
            return []
        try:
            rows = self.photos_conn.execute(
                'SELECT DISTINCT ph.group_id, ph.path, ph.caption, ph.is_primary, ph.source_id '
                'FROM photo_people pp JOIN photos ph ON pp.path = ph.path '
                'WHERE pp.person_ref = ? AND ph.path NOT LIKE ?',
                (pid, f'{_MISSING_PREFIX}%'),
            ).fetchall()
        except sqlite3.DatabaseError:
            return []
        # Standalone: exclude photos from withheld sources.
        # photos.source_id is stored lowercase by normalize_id; compare case-insensitively.
        if not self.linked:
            source_pages_lower = {s.lower() for s in self.source_pages}
            rows = [
                r for r in rows
                if not r['source_id'] or r['source_id'].lower() in source_pages_lower
            ]
        # Standalone: exclude groups that are also tagged to a living person.
        if not self.linked:
            safe: set[str] = set()
            unsafe: set[str] = set()
            for r in rows:
                gkey = r['group_id'] or r['path']
                if gkey in safe or gkey in unsafe:
                    continue
                try:
                    if r['group_id']:
                        co_refs = self.photos_conn.execute(
                            'SELECT DISTINCT pp.person_ref FROM photo_people pp '
                            'JOIN photos ph ON pp.path = ph.path WHERE ph.group_id = ?',
                            (r['group_id'],),
                        ).fetchall()
                    else:
                        co_refs = self.photos_conn.execute(
                            'SELECT DISTINCT person_ref FROM photo_people WHERE path = ?',
                            (r['path'],),
                        ).fetchall()
                except sqlite3.DatabaseError:
                    safe.add(gkey)
                    continue
                has_living = any(
                    self._person_is_redacted(self.person_meta[ref['person_ref']])
                    for ref in co_refs if ref['person_ref'] in self.person_meta
                )
                (unsafe if has_living else safe).add(gkey)
            rows = [r for r in rows if (r['group_id'] or r['path']) not in unsafe]

        # One representative per group: prefer the group's primary.
        best: dict[str, sqlite3.Row] = {}
        for r in rows:
            key = r['group_id'] or r['path']
            if key not in best or (r['is_primary'] and not best[key]['is_primary']):
                best[key] = r
        return [e for e in (self._photo_entry(r, page_dir) for r in best.values()) if e]

    def _photo_entry(self, row: sqlite3.Row, page_dir: Path) -> dict | None:
        """One photo-strip entry. Standalone makes an EXIF-stripped derivative;
        linked points at the real file. A missing/unprocessable image is dropped
        from the strip (with a warning in standalone) rather than shown broken."""
        try:
            resolved = resolve_path(row['path'], self.fha_config, self.archive_root)
        except Exception:
            return None
        caption = (row['caption'] or '').strip()
        if not resolved.exists():
            return None
        if self.linked:
            href = self._asset_href(resolved, page_dir)
            return {'href': href, 'full_href': href, 'caption': caption}
        if not _PIL_AVAILABLE:
            return None
        dest = self._media_dest(row['path'], 'people')
        if not _make_derivative(resolved, dest):
            self.messages.append(f'WARNING: could not build a web image for {row["path"]} (omitted from photo strip)')
            return None
        href = _rel_href(dest, page_dir)
        return {'href': href, 'full_href': href, 'caption': caption}

    # - profile photo (a person's chosen main portrait) -

    def _profile_photo_file(self, pid: str) -> Path | None:
        """The publishable image file for a person's `profile_photo:` field - a
        fresh EXIF-stripped derivative in standalone, the original in linked - or
        None when unset, unresolvable, or withheld. Resolved once per person and
        reused across their page and every tree node they appear in."""
        if pid not in self._profile_photo_cache:
            self._profile_photo_cache[pid] = self._resolve_profile_photo(pid)
        return self._profile_photo_cache[pid]

    def _profile_photo_href(self, pid: str, page_dir: Path) -> str | None:
        f = self._profile_photo_file(pid)
        return self._asset_href(f, page_dir) if f else None

    def _resolve_asset_path(self, ref: str) -> Path | None:
        """Best-effort resolve a human-written photo reference to a file on disk:
        a path under a configured root, an archive-relative path, or a bare
        filename found under the photos root. Lets hero / embeds / profile_photo
        work without the (exiftool-based) photo catalog."""
        ref = str(ref).strip().replace('\\', '/')
        if not ref:
            return None
        cands: list[Path] = []
        try:
            cands.append(resolve_path(ref, self.fha_config, self.archive_root))
        except Exception:  # noqa: BLE001
            pass
        cands.append(self.archive_root / ref)
        roots = self.fha_config.get('roots')
        photos_root = roots.get('photos') if isinstance(roots, dict) else None
        if photos_root:
            pr = Path(photos_root)
            if not pr.is_absolute():
                pr = self.archive_root / pr
            cands.append(pr / ref)
            cands.append(pr / ref.rsplit('/', 1)[-1])
        for c in cands:
            try:
                if c and Path(c).is_file():
                    return Path(c)
            except OSError:
                continue
        # Documented layout is photos/<year>/<file>. When the ref is a bare
        # filename (no directory component) and the direct paths above missed,
        # scan the photos root for a unique basename match so a hero /
        # profile_photo written as "foo.jpg" still resolves without a photo
        # catalog. Restricted to image suffixes to cap traversal cost.
        #
        # `photos_ignore:` (#35) prunes this guess exactly as it prunes the
        # scan: a bulk photo-service export is not the family library, so a
        # bare filename must not be answered from one - the file was never
        # cataloged, nobody reviewed who is in it, and the ambiguity warning
        # below would fire on names the human never meant to offer. An
        # explicitly written path is a different matter and is honored above:
        # ignoring a folder says "don't go looking in here", not "this file
        # may never be published".
        if photos_root and '/' not in ref and Path(ref).suffix.lower() in _IMAGE_SUFFIXES:
            pr = Path(photos_root)
            if not pr.is_absolute():
                pr = self.archive_root / pr
            try:
                is_ignored = photos_ignore_matcher(photos_ignore_patterns(self.fha_config))
            except RuntimeError:
                # A malformed photos_ignore: is the scan's error to report in
                # plain words; the site just declines to guess rather than
                # failing a whole build over a setting it only reads.
                return None
            #
            # Walked with an error seam, not `rglob`. The guard this loop
            # exists to enforce is "only publish a bare filename when exactly
            # ONE file answers to it", and a folder that will not list makes
            # two matches look like one - so the site would publish, on the
            # front page, a photo chosen by which folder happened to open.
            # That is the guard failing OPEN, and a published photo cannot be
            # unpublished. When the walk is incomplete the answer is
            # 'I don't know', which here means no hero image and a line saying
            # why.
            unreadable: list[Path] = []
            try:
                matches: list[Path] = []
                if pr.is_dir():
                    for m in walk_files(
                            pr, on_error=unreadable_dir_recorder(unreadable)):
                        # `PurePath.match` on a bare name is exactly what
                        # `rglob(ref)` matched (same fnmatch rules, same
                        # platform case-sensitivity), so a ref that happens to
                        # carry a `*` or `?` still behaves as it always did.
                        if not m.match(ref):
                            continue
                        try:
                            rel = m.relative_to(pr).as_posix()
                        except ValueError:  # pragma: no cover - walk result is under pr
                            continue
                        if _under_ignored_path(rel, is_ignored):
                            continue
                        if m.is_file():
                            matches.append(m)
                            if len(matches) > 1:
                                break
                # Only a folder this guess would have ANSWERED from can spoil
                # the count. `unreadable` is collected over the whole photos
                # root, but the loop above refuses every match under
                # `photos_ignore:` anyway, so a shut folder inside a
                # `Flickr Export` subtree cannot hide a second candidate: there
                # are no candidates in there to hide. Dropping the hero photo
                # over it is a refusal the guard has not earned - and
                # `photos_ignore:` exists precisely to name bulk exports, which
                # are exactly the things that sit on drives that come and go.
                unreadable = [
                    d for d in unreadable
                    if not _under_ignored_dir(d, pr, is_ignored)
                ]
                if unreadable and len(matches) < 2:
                    self.messages.append(
                        f'WARNING: photo reference {ref!r} was not used: '
                        f'{len(unreadable)} folder(s) in your photos folder '
                        f'could not be opened, so there is no way to tell '
                        f'whether more than one photo has that name. Reconnect '
                        f'the drive (or fix the folder), or write the '
                        f'reference as `<year>/{ref}` to name the exact file.')
                    return None
                if len(matches) == 1:
                    return matches[0]
                if len(matches) > 1:
                    self.messages.append(
                        f'WARNING: photo reference {ref!r} matched multiple files under photos root; '
                        'qualify with a subdirectory (e.g. `<year>/foo.jpg`).')
            except OSError:
                pass
        return None

    def _resolve_sid_image(self, ref: str) -> Path | None:
        """Resolve an `S-id` photo reference through the main index's
        `source_files` table (no photo catalog needed). The source page and the
        rest of the site already read this table, so an S-id must always
        resolve - even when `.cache/photos.sqlite` is absent or stale - so long
        as its source is publishable. Returns None if the id is not S-shaped,
        the source has no attached file on disk, or the source is withheld."""
        if not re.match(r'(?i)^s-[0-9a-z]+$', ref.strip()):
            return None
        sid = normalize_id(ref.strip())
        if not self.linked and sid not in self.source_pages:
            return None
        try:
            rows = self.conn.execute(
                'SELECT path FROM source_files WHERE source_id = ? '
                'AND COALESCE(exists_on_disk,1) = 1 '
                'ORDER BY COALESCE(derived,0), path',
                (sid,),
            ).fetchall()
        except sqlite3.DatabaseError:
            return None
        for r in rows:
            p = r['path']
            if not p:
                continue
            if Path(p).suffix.lower() not in _IMAGE_SUFFIXES:
                continue
            try:
                cand = resolve_path(p, self.fha_config, self.archive_root)
            except Exception:  # noqa: BLE001
                cand = self.archive_root / p
            try:
                if cand and Path(cand).is_file():
                    return Path(cand)
            except OSError:
                continue
        return None

    def _resolve_image_source(self, ref: str) -> Path | None:
        """The on-disk source file for a photo reference. Prefers a catalogued
        photo (S-id or indexed path), applying the strip's privacy gate; else a
        hand-written path/filename on disk. If a photo catalog is available in
        standalone mode, an uncatalogued disk hit is fail-closed - the author who
        wrote a bare filename should either catalog the file or accept the safe
        default; only when there is no catalog at all does a raw disk lookup
        publish without a co-living gate. None if nothing resolves."""
        cat = self._resolve_photo_ref(ref)
        if cat:
            # A catalog match that the privacy gate rejects must NOT fall through to
            # the on-disk fallback: that would re-publish the very file the gate meant
            # to withhold (a co-tagged living/restricted person, a withheld source).
            if not self.linked and not self._photo_is_public(cat):
                return None
            try:
                r = resolve_path(cat, self.fha_config, self.archive_root)
                if r and r.exists():
                    return r
            except Exception:  # noqa: BLE001
                pass
        # An S-id resolves via `source_files` too, so a stale/absent photo
        # catalog does not silently drop hero / embed / profile images.
        sid_hit = self._resolve_sid_image(ref)
        if sid_hit is not None:
            return sid_hit
        disk = self._resolve_asset_path(ref)
        if disk is None:
            return None
        # Standalone + a catalog exists: a disk hit that has no catalog entry
        # (or one that fails the privacy gate) is fail-closed. If there is no
        # catalog at all (photos_conn is None) the hand-written path is the
        # deliberate publish choice the caller made.
        if not self.linked and self.photos_conn is not None:
            cat_path = self._catalog_path_for_disk(disk)
            if cat_path is None:
                return None
            if not self._photo_is_public(cat_path):
                return None
        return disk

    def _catalog_path_for_disk(self, disk: Path) -> str | None:
        """The catalog-stored path (if any) that names the file at `disk`. Tries
        the archive-relative path first, then a basename LIKE. Returns None when
        the file has no catalog entry.

        A row reconcile flagged 'MISSING:' is a valid answer here - this is the
        "which catalog row describes this file" question, not "can it be
        opened", and the file in hand may be the very one that came back - but
        a still-current row is preferred when the basename matches both."""
        if self.photos_conn is None:
            return None
        try:
            rel = str(disk.resolve().relative_to(self.archive_root.resolve()))
        except (OSError, ValueError):
            rel = None
        try:
            if rel:
                row = self.photos_conn.execute(
                    'SELECT path FROM photos WHERE path = ?', (rel,)).fetchone()
                if row:
                    return row['path']
            row = self.photos_conn.execute(
                'SELECT path FROM photos WHERE path = ? OR path LIKE ? '
                'ORDER BY (path LIKE ?), path',
                (disk.name, '%/' + disk.name, f'{_MISSING_PREFIX}%')).fetchone()
            if row:
                return row['path']
        except sqlite3.DatabaseError:
            return None
        return None

    def _resolve_profile_photo(self, pid: str) -> Path | None:
        """Read the person's `profile_photo:` field, resolve it (a catalogued
        photo or a path/filename on disk), and produce a small derivative. Any
        miss is a warn-and-skip, never a build failure."""
        meta = self.person_meta.get(pid)
        if meta is None:
            return None
        # A living/redacted person gets no portrait in the shared snapshot.
        if not self.linked and self._person_is_redacted(meta):
            return None
        try:
            rec = read_record(self.archive_root / meta['path'],
                               on_decode_error=_ignore_decode_error)
        except Exception as e:  # noqa: BLE001 - defensive; see _ignore_decode_error
            self.messages.append(
                f'WARNING: could not read {meta["path"]} ({e}); skipping the '
                f'profile photo for {fmt_id_display(pid)}.')
            return None
        if rec.get('undecodable'):
            # Unlike the workbench-only hypothesis-tie helpers above, this one
            # is memoized per pid (`_profile_photo_file`'s cache) and reached
            # for ancestors/spouses/children drawn into ANY tree on the site,
            # not only the broken record's own page - `prepare()`'s file-scan
            # warning (standalone only) may never run and no other read of
            # this file is guaranteed, so this is worth naming once here.
            self.messages.append(
                f'WARNING: could not read {meta["path"]} (this file isn\'t '
                f'saved as UTF-8 text - a Windows editor\'s default encoding, '
                f'often cp1252, is the usual cause); skipping the profile '
                f'photo for {fmt_id_display(pid)}. Open it and save it again '
                f'choosing UTF-8, then run `fha site` again.')
            return None
        ref = str((rec.get('meta') or {}).get('profile_photo') or '').strip()
        if not ref:
            return None
        resolved = self._resolve_image_source(ref)
        if resolved is None:
            self.messages.append(
                f'WARNING: profile_photo for {fmt_id_display(pid)} ("{ref}") matched no photo; skipped.')
            return None
        if self.linked:
            return resolved
        if not _PIL_AVAILABLE:
            return None
        dest = self._media_dest(ref, 'profiles')
        if _make_derivative(resolved, dest, max_px=_PROFILE_MAX_PX):
            return dest
        self.messages.append(
            f'WARNING: could not build a web image for profile_photo {ref} ({fmt_id_display(pid)}); skipped.')
        return None

    def _resolve_photo_ref(self, ref: str) -> str | None:
        """Map a `profile_photo:` value to a stored photo path via the catalog.
        Tries, in order: an S-id (the source's primary photo), the exact stored
        path, then a basename match (so a moved file still resolves). Prefers a
        photo that is still on disk, then the group's primary variant: the
        answer here is going to be opened, and a vanished variant's row keeps
        its `is_primary` flag, so ranking on primary alone would hand back a
        file that cannot be read while a good sibling sat behind it."""
        if self.photos_conn is None:
            return None
        r = ref.strip().replace('\\', '/')

        def pick(sql: str, params: tuple) -> str | None:
            try:
                rows = self.photos_conn.execute(sql, params).fetchall()
            except sqlite3.DatabaseError:
                return None
            if not rows:
                return None
            rows = sorted(
                rows,
                key=lambda x: (1 if _is_missing_key(x['path']) else 0,
                               0 if x['is_primary'] else 1),
            )
            return rows[0]['path']

        if re.match(r'(?i)^s-[0-9a-z]+$', r):
            hit = pick('SELECT path, is_primary FROM photos WHERE lower(source_id) = lower(?)', (r,))
            if hit:
                return hit
        hit = pick('SELECT path, is_primary FROM photos WHERE path = ?', (r,))
        if hit:
            return hit
        base = r.rsplit('/', 1)[-1]
        return pick('SELECT path, is_primary FROM photos WHERE path = ? OR path LIKE ?',
                    (base, '%/' + base))

    def _photo_is_public(self, path: str) -> bool:
        """Standalone gate for a single photo: its source (if any) must be
        published, and it must not co-depict a living/redacted person. Mirrors
        the photo-strip rules so a profile portrait can never leak either."""
        if self.photos_conn is None:
            return False
        try:
            row = self.photos_conn.execute(
                'SELECT source_id, group_id FROM photos WHERE path = ?', (path,)).fetchone()
        except sqlite3.DatabaseError:
            return False
        if row is None:
            return False
        src = (row['source_id'] or '').lower()
        if src and src not in {s.lower() for s in self.source_pages}:
            return False
        try:
            if row['group_id']:
                refs = self.photos_conn.execute(
                    'SELECT DISTINCT pp.person_ref FROM photo_people pp '
                    'JOIN photos ph ON pp.path = ph.path WHERE ph.group_id = ?',
                    (row['group_id'],)).fetchall()
            else:
                refs = self.photos_conn.execute(
                    'SELECT DISTINCT person_ref FROM photo_people WHERE path = ?', (path,)).fetchall()
        except sqlite3.DatabaseError:
            return False
        for ref in refs:
            m = self.person_meta.get(ref['person_ref'])
            if m is not None and self._person_is_redacted(m):
                return False
        return True

    def _image_href(self, ref: str, page_dir: Path, subdir: str) -> str | None:
        """Resolve a photo reference (S-id, path, or filename) to a publishable
        image href, or None. Shared by prose embeds and the homepage hero: a
        catalogued photo goes through the strip's privacy gate; a hand-written
        path/filename resolves directly on disk (no catalog / exiftool needed).
        Standalone emits an EXIF-stripped derivative; linked points at the file."""
        resolved = self._resolve_image_source(ref)
        if resolved is None:
            return None
        if self.linked:
            return self._asset_href(resolved, page_dir)
        if not _PIL_AVAILABLE:
            return None
        dest = self._media_dest(ref, subdir)
        return _rel_href(dest, page_dir) if _make_derivative(dest=dest, src=resolved) else None

    def _render_embed(self, target: str, caption: str, page_dir: Path) -> str:
        """A `![[S-id|Caption]]` prose embed → a responsive <figure>. The image is
        capped in height by CSS so a large scan never blows up the page. An
        unresolvable or withheld reference renders nothing (never a raw id).

        `_prose_to_html` calls `render_embed` (this method) BEFORE
        `_inline_html`'s per-span scrub (#144 finding 3) ever runs over the
        block, so the caption must be scrubbed of internal-only encoding
        (`_scrub_internal_encoding`, #140) right here - a caption is
        reader-facing prose exactly like a link's label, and an unscrubbed
        claim id would otherwise leak into both the visible <figcaption>
        text and the image's alt attribute (P2, PR #158 follow-up). The
        embed TARGET is never scrubbed - it is an id/path, not prose."""
        href = self._image_href(target, page_dir, 'embeds')
        if not href:
            self.messages.append(f'WARNING: embed {target!r} matched no publishable photo; skipped.')
            return ''
        caption = _scrub_internal_encoding(caption) if caption else caption
        cap = _escape(caption) if caption else ''
        # `alt` is an HTML attribute - a caption like `" onerror="alert(1)` would
        # break out of the `_escape(quote=False)` body form. Quote-aware escaping
        # for the attribute; keep the body form for `<figcaption>`.
        cap_attr = html.escape(caption, quote=True) if caption else ''
        figcap = f'<figcaption>{cap}</figcaption>' if cap else ''
        return (f'<figure class="embed"><img class="embed-img" src="{html.escape(href, quote=True)}" '
                f'alt="{cap_attr}" loading="lazy">{figcap}</figure>')

    # - place page (M8.3) -

    def build_place_page(self, lid: str) -> None:
        """Render one place page (TOOLING §12 / M8.3): name, coords (an embedded
        OpenStreetMap view plus the plain map link - owner decision, review
        2026-07-16; the iframe degrades to nothing offline and the link always
        works), dated `history:`, the registry's `notes:` prose, claims naming
        the place, contained micro-places (`within:` children), and the people
        most often associated with it. People links follow the standard
        redaction rule, and the people-frequency list omits redacted persons
        entirely so a standalone place page never links to - or even names - a
        living person."""
        row = self.place_meta[lid]
        page_dir = self.places_dir
        self._footnotes = None        # place-page sources render as named links, not footnotes

        lat, lon = row['lat'], row['lon']
        map_url = None
        map_embed_url = None
        if lat is not None and lon is not None:
            map_url = f'https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=12/{lat}/{lon}'
            # The embed endpoint needs an explicit bounding box; roughly a
            # town-scale window around the pin reads best for family places.
            d_lat, d_lon = 0.03, 0.05
            map_embed_url = (
                'https://www.openstreetmap.org/export/embed.html'
                f'?bbox={lon - d_lon}%2C{lat - d_lat}%2C{lon + d_lon}%2C{lat + d_lat}'
                f'&layer=mapnik&marker={lat}%2C{lon}')

        alt_names = [
            r['alt_name'] for r in self.conn.execute(
                'SELECT alt_name FROM place_names WHERE place_id = ? ORDER BY alt_name', (lid,))
            if r['alt_name']
        ]
        history = [
            {'period': r['period_edtf'] or '', 'hierarchy': r['hierarchy'] or ''}
            for r in self.conn.execute(
                'SELECT period_edtf, date_min, hierarchy FROM place_history WHERE place_id = ? '
                "ORDER BY CASE WHEN date_min IS NULL OR date_min = '' THEN 1 ELSE 0 END, date_min", (lid,))
        ]

        living_filter = (
            '' if self.linked else
            "AND NOT EXISTS ("
            "  SELECT 1 FROM claim_persons cp2 JOIN persons p ON cp2.person_id = p.id "
            "  WHERE cp2.claim_id = c.id AND p.living IN ('true','unknown')"
            ")"
        )
        # Same audience split as the person timeline (owner decision 2026-07-22):
        # public = accepted only; linked/workbench keeps needs-review, marked.
        place_status_filter = ("c.status IN ('accepted','needs-review')" if self.linked
                               else "c.status = 'accepted'")
        claim_rows = self.conn.execute(
            "SELECT c.id, c.type, c.value, c.date_edtf, c.date_min, c.source_id, "
            "c.status, c.confidence, c.reviewed FROM claims c "
            f"WHERE c.place_id = ? AND {place_status_filter} {living_filter} "
            "ORDER BY CASE WHEN c.date_min IS NULL OR c.date_min = '' THEN 1 ELSE 0 END, c.date_min ASC",
            (lid,),
        ).fetchall()
        # Standalone: also withhold events whose only source is restricted/living-linked,
        # and a restricted claim regardless of its source.
        if not self.linked:
            # Match the person-timeline policy (`_source_hard_restricted`): show
            # the event with its citation redacted when the source is merely
            # withheld (names a living person), and omit only a restricted claim
            # or a hard-restricted source. Using `source_pages` here instead
            # would drop the same fact from the place page while the person
            # page still shows it.
            claim_rows = [c for c in claim_rows
                          if not self._source_hard_restricted(c['source_id'])
                          and normalize_id(str(c['id'])) not in self.restricted_claims]
        claims = []
        person_freq: dict[str, int] = {}
        for c in claim_rows:
            person_rows = self.conn.execute(
                'SELECT person_id FROM claim_persons WHERE claim_id = ? ORDER BY position', (c['id'],)
            ).fetchall()
            for p in person_rows:
                person_freq[p['person_id']] = person_freq.get(p['person_id'], 0) + 1
            claims.append({
                'type': c['type'],
                # Reader-facing cell: scrubbed of internal-only encoding, same
                # as the source page's claims table (#144 finding 4). The
                # place page's Events table has no workbench edit-prefill for
                # a claim value, so unlike build_source_page there is no raw
                # counterpart to keep alongside it.
                'value': _scrub_internal_encoding(c['value'] or ''),
                'date': c['date_edtf'] or '',
                'persons_html': self._markup(
                    ', '.join(self._person_link(p['person_id'], page_dir) for p in person_rows)),
                'source_html': self._markup(self._source_link(c['source_id'], page_dir)) if c['source_id'] else '',
                'status': c['status'], 'confidence': c['confidence'] or '',
                'parked': c['reviewed'] or '',
            })

        # People-frequency list: links only, redacted persons omitted entirely.
        people = []
        for person_id, count in sorted(person_freq.items(), key=lambda kv: (-kv[1], kv[0])):
            meta = self.person_meta.get(person_id)
            if meta is None:
                continue
            if not self.linked and self._person_is_redacted(meta):
                continue
            people.append({'html': self._markup(self._person_link(person_id, page_dir)), 'count': count})

        micro = []
        for r in self.conn.execute('SELECT id FROM places WHERE within = ? ORDER BY id', (lid,)):
            child = r['id']
            if child in self.place_meta:
                micro.append(self._markup(self.render_token(fmt_id_display(child), page_dir)))

        # The registry's `notes:` prose - reference context (SPEC §15, loose
        # citations welcome), rendered in both modes; [[links]] resolve like
        # any prose. Workbench mode also splits it into its append-log
        # entries (same grammar as the person/source logs) so each note
        # carries its own edit button driving `fha places edit-note`.
        notes_html = None
        notes_entries: list[dict] = []
        notes_raw = str(row['notes'] or '').strip()
        if notes_raw:
            render = lambda tok, disp=None: self.render_token(tok, page_dir, disp)  # noqa: E731
            embed = lambda t, c: self._render_embed(t, c, page_dir)  # noqa: E731
            notes_html = self._markup(_prose_to_html(
                notes_raw, render, embed, drop_private=not self.linked))
            if self.workbench:
                notes_entries = [
                    {'html': self._markup(_prose_to_html(
                        e, render, embed, drop_private=not self.linked)), 'raw': e}
                    for e in split_log_entries(notes_raw)]

        ctx = {
            'display_id': fmt_id_display(lid), 'name': row['name'] or fmt_id_display(lid),
            'hierarchy': row['hierarchy'] or '', 'map_url': map_url,
            'map_embed_url': map_embed_url,
            'alt_names': alt_names, 'history': history, 'claims': claims,
            'people': people, 'micro': micro, 'notes_html': notes_html,
            'notes_entries': notes_entries,
            # Workbench-only prefills (template gates on `workbench`): the
            # current values in exactly the plain shapes the edit modals and
            # `fha places set` speak - "lat, lon", one alias per line (a
            # comma join could not round-trip "Washington, D.C." - P2 codex
            # finding, round 2, PR #31), one "PERIOD | HIERARCHY" line per
            # history entry.
            'lat': lat, 'lon': lon,
            'aka_lines': '\n'.join(alt_names),
            'history_lines': '\n'.join(
                (f"{h['period']} | {h['hierarchy']}" if h['period'] else h['hierarchy'])
                for h in history),
        }
        self._write_page(self.places_dir / _page_filename(lid), 'place.html',
                         {'place': ctx, 'root_prefix': '..'})

    # - discoveries page (M8.3) -

    def build_discoveries_page(self) -> None:
        """Render `notes/discoveries.md` as the discoveries page (TOOLING §12 /
        M8.3). P-id/S-id mentions are linked (and redacted) by the shared token
        renderer, so a living person named in a discovery never leaks here under
        standalone. A missing or empty file yields a plain "nothing logged yet"
        page rather than a broken link from the home teaser."""
        body, _entries = self._read_discoveries()
        page_dir = self.out_dir
        render = lambda tok, disp=None: self.render_token(tok, page_dir, disp)  # noqa: E731
        # An `![[S-id|Cap]]` in discoveries prose renders as a `<figure>` on the
        # home teaser; keep the same shape here so the full page matches.
        embed = lambda t, c: self._render_embed(t, c, page_dir)  # noqa: E731
        content_html = _prose_to_html(body, render, embed, drop_private=not self.linked) if body else ''
        self._write_page(self.out_dir / 'discoveries.html', 'discoveries.html', {
            'content_html': self._markup(content_html) if content_html else None,
            'root_prefix': '.',
        })

    def _read_discoveries(self) -> tuple[str, list[str]]:
        """Read notes/discoveries.md and return (body_without_leading_H1,
        recent_entry_chunks). An entry is a `##`/`###` section or a top-level
        `-` bullet - the dated, ref-carrying shape TOOLING §15a appends. The
        schema is loose by design, so this is tolerant: no recognizable entries
        means an empty teaser, never an error. The last five chunks (most
        recently appended) are returned for the home-page teaser. Memoized: the
        discoveries page and the home teaser both call this, but the file is
        parsed once per build."""
        if self._discoveries is not None:
            return self._discoveries
        path = self.archive_root / 'notes' / 'discoveries.md'
        # NOT a plain `read_text` with an `except OSError` (#68): a
        # `UnicodeDecodeError` is a ValueError, so a discoveries.md saved in
        # another codepage (cp1252, a Windows editor's default) sailed straight
        # past that guard and raised out of `run_site` itself - past that
        # function's contract to RETURN a Result, so the CLI fell back to
        # `fha.py`'s catch-all (raw codec text, exit 3) and serve's workbench
        # rebuild, which calls `run_site` directly, took the exception. The
        # only read in this module that could still do that, because it is the
        # only one that does not go through `read_record`.
        # `read_text_or_report` splits the two failures the way this method
        # already wants them split: a missing/unreadable file stays the silent
        # skip it has always been (running without a discoveries log is
        # ordinary), and a bad decode is reported. Memoized like the rest of
        # this method, so the discoveries page and the home teaser - both
        # callers - earn exactly one warning between them.
        undecodable: list[Path] = []
        text = read_text_or_report(path, on_decode_error=undecodable.append)
        if text is None:
            if undecodable:
                self.messages.append(
                    "WARNING: could not read notes/discoveries.md (this file "
                    "isn't saved as UTF-8 text - a Windows editor's default "
                    "encoding, often cp1252, is the usual cause); the "
                    'discoveries page and the home page teaser are empty. Open '
                    'it and save it again choosing UTF-8, then run `fha site` '
                    'again.')
            self._discoveries = ('', [])
            return self._discoveries
        # Exclude unaccepted AI-DRAFT prose before publishing, same as person
        # prose (_person_prose): the standalone site is external output, so a
        # draft must never leak here. Fail closed on a damaged marker - withhold
        # the whole page rather than emit half-parsed draft text or a raw marker.
        text, problem = strip_unaccepted_drafts(text)
        if problem is not None:
            self.messages.append(
                'WARNING: a draft marker in notes/discoveries.md is damaged '
                f'({problem}) - the discoveries page is withheld from the site.')
            self._discoveries = ('', [])
            return self._discoveries
        # Apply the `<!-- private -->` fence to the WHOLE file before splitting
        # into entry chunks. Otherwise an opener that sits above a `##` heading
        # gets stranded in the previous chunk, and the entry it was meant to
        # fence keeps only the trailing `<!-- /private -->` - leaking through
        # the teaser and the discoveries page on standalone builds.
        text = apply_private_fence(text, drop=not self.linked)
        lines = text.replace('\r\n', '\n').split('\n')
        # Drop a single leading H1 (the page supplies its own title).
        if lines and lines[0].startswith('# '):
            lines = lines[1:]
        body = '\n'.join(lines).strip()

        # Split into entry chunks: prefer ##/### sections, else top-level bullets.
        chunks: list[str] = []
        section_starts = [i for i, ln in enumerate(lines) if re.match(r'^#{2,3}\s+', ln)]
        if section_starts:
            bounds = section_starts + [len(lines)]
            for a, b in zip(section_starts, bounds[1:]):
                chunk = '\n'.join(lines[a:b]).strip()
                if chunk:
                    chunks.append(chunk)
        else:
            chunks = [ln.strip() for ln in lines if _LIST_RE.match(ln)]
        self._discoveries = (body, chunks[-5:])
        return self._discoveries

    # - interactive tree (M8.5) -

    def _person_vitals(self, pid: str) -> dict:
        """First accepted birth/death `date_edtf` for a person, for tree labels.
        Mirrors `fha views tree`'s node vitals (TOOLING §7 D3).

        Negated claims are excluded (COALESCE(c.negated, 0) = 0): a negated
        birth/death is a confirmed absence, not a date to label a pedigree node
        with. Same posture as `_person_summary`.

        So are claims that are a record of somebody else the claim also names
        (`_claim_is_own_vital`, #126). A chart node's life dates are the
        shortest, most quotable fact on the page, and reading them off any
        birth/death claim naming the person is what produced nodes labelled
        `1955-1916` and great-grandparents charted with their own child's birth
        year. `ORDER BY c.id` makes the surviving pick deterministic, as it
        does in `_person_summary` - without it the committed example fixtures
        churn between rebuilds on sqlite's rowid order."""
        vitals = {'birth': None, 'death': None}
        for r in self.conn.execute(
            "SELECT c.id, c.type, c.date_edtf, c.source_id FROM claims c JOIN claim_persons cp ON c.id = cp.claim_id "
            "WHERE cp.person_id = ? AND c.type IN ('birth','death') AND c.status = 'accepted' "
            "AND COALESCE(c.negated, 0) = 0 ORDER BY c.id",
            (pid,),
        ):
            if not self._claim_is_own_vital(pid, r['id'], r['type']):
                continue
            # Standalone: show a (deceased) person's date even when its source is
            # merely withheld - the node carries no citation to redact, and only
            # living people are redacted outright. Drop only a restricted claim or a
            # hard-restricted source (DNA / by-request / publication_ok:false).
            if not self.linked and normalize_id(str(r['id'])) in self.restricted_claims:
                continue
            if not self.linked and self._source_hard_restricted(r['source_id']):
                continue
            if vitals.get(r['type']) is None:
                vitals[r['type']] = r['date_edtf'] or None
        return vitals

    def _apex_ancestor(self, root_pid: str) -> str | None:
        """Walk `parent` edges up from `root_pid` and return the CLOSEST
        non-living, non-redacted ancestor reached - the standalone home
        pedigree's redaction-safe hub fallback (#115). Returns None when no
        eligible ancestor exists at all (root_pid's whole recorded line is
        living/unknown/restricted, or every tie to it is unpublishable, or
        nothing is recorded above root_pid and root_pid is itself
        ineligible) - the caller's cue to fall back to a blank hub-only
        render (`_build_family_wings` also withholds root_pid's own spouse/
        children/siblings once it is itself redacted - review fix, PR #152)
        with an explanatory note, rather than a page with nothing on it.

        BFS explores nearer ancestors before farther ones (parents, then
        grandparents, ...), one whole generation at a time, each generation's
        candidates tie-broken by id for a stable, deterministic pick - so the
        result is always the CLOSEST eligible ancestor to root_pid, not
        necessarily that line's true apex (a nearer hub keeps more of the
        pedigree - siblings, first cousins - inside the default rendering
        depth than walking all the way to the most distant recorded ancestor
        would). `root_pid` itself is checked first and returned unchanged when
        it is already eligible - the common case, and the one where no walk
        at all is needed.

        Before #115 this walked ALL THE WAY to the single deepest recorded
        ancestor (ties broken low-id) and was the home page's DESCENDANTS-mode
        seed, fanning the old collapsible-tree explorer forward across the
        whole line; that explorer moved to a per-person opt-in link (see
        `build_person_page`'s `descendants_tree`), so this function lost its
        only caller and is repurposed here rather than deleted, per #115's
        design. The graph walk (BFS over `parent` edges) is UNCHANGED; only
        the selection rule (closest-eligible instead of deepest-of-all) and
        the redaction check are new.

        EDGE eligibility (review fix, PR #152): a candidate is not just the
        far end of SOME parent edge - it is the far end of a specific tie,
        and that tie must itself be publishable, exactly like every other
        ancestor/descendant walk in this file (`_build_ahnentafel`,
        `_build_tree_data`) already requires via `_has_public_claim`. Without
        this, a parent tie backed only by a restricted claim or a claim
        sourced exclusively from a hard-restricted source (DNA/by-request/
        publication_ok:false) - the exact case that mechanism exists to keep
        off the public site - could still promote that ancestor to be the
        home page's PUBLIC-FACING hub: their real name, dates, and a link to
        their own page, once removed from a living seed by nothing sturdier
        than the very tie the marker says not to publish. A non-public edge
        is skipped outright (`continue` before `other` is ever added to
        `seen`/the next level), so - like the other walks - it also cannot be
        used to reach further ancestors THROUGH it: there is no public path
        across an unpublishable tie, so nothing beyond it is reachable
        either. `root_pid` itself needs no such check (it has no incoming
        edge to cross to be considered)."""
        if root_pid in self.person_meta and not self._person_is_redacted(self.person_meta[root_pid]):
            return root_pid
        seen = {root_pid}
        queue = deque([root_pid])
        while queue:
            level: list[str] = []
            for _ in range(len(queue)):
                cur = queue.popleft()
                for r in self.conn.execute(
                    "SELECT DISTINCT other_id FROM relationships WHERE person_id = ? AND rel = 'parent'",
                    (cur,),
                ):
                    other = r['other_id']
                    if other in seen:
                        continue
                    if not self.linked and not self._has_public_claim(cur, other):
                        continue
                    seen.add(other)
                    level.append(other)
            for pid in sorted(level):
                meta = self.person_meta.get(pid)
                if meta is not None and not self._person_is_redacted(meta):
                    return pid
            queue.extend(level)
        return None

    def _tree_node(self, pid: str, page_dir: Path) -> dict:
        """One neutral-JSON tree node, with redaction and a `url` applied here
        (server-side) so a standalone tree file never carries a living person's
        name, vitals, or a link to a page that wasn't generated."""
        meta = self.person_meta.get(pid)
        display = fmt_id_display(pid)
        if meta is None:
            return {'p_id': display, 'name': display, 'sex': None,
                    'vitals': {'birth': None, 'death': None}, 'url': None}
        if not self.linked and self._person_is_redacted(meta):
            return {'p_id': display, 'name': _LIVING_LABEL, 'sex': None,
                    'vitals': {'birth': None, 'death': None}, 'url': None}
        url = None
        if pid in self.person_pages:
            url = _rel_href(self.persons_dir / _page_filename(pid), page_dir)
        return {'p_id': display, 'name': meta['name'] or display, 'sex': meta['sex'],
                'vitals': self._person_vitals(pid), 'url': url,
                'photo': self._profile_photo_href(pid, page_dir)}

    def _tree_relationship_rows(self, pid: str, rel: str) -> list:
        """One person's `relationships` rows for direction `rel`, memoized for
        the whole build (#152 review fix, P2, finding 1 - see
        `_tree_edges_cache`'s comment in `__init__` and `_DESCENDANT_TREE_MAX_
        HOPS`'s comment for why this matters: it is the fix that lets the BFS
        below stay unbounded without re-paying an O(N^2) query cost)."""
        key = (pid, rel)
        rows = self._tree_edges_cache.get(key)
        if rows is None:
            rows = self.conn.execute(
                '''SELECT DISTINCT r.other_id, r.claim_id, c.subtype
                   FROM relationships r LEFT JOIN claims c ON r.claim_id = c.id
                   WHERE r.person_id = ? AND r.rel = ?''',
                (pid, rel),
            ).fetchall()
            self._tree_edges_cache[key] = rows
        return rows

    def _build_tree_data(self, seed: str, mode: str, max_hops: int | None, page_dir: Path) -> dict:
        """BFS the `relationships` graph from `seed` and emit the neutral tree
        JSON (TOOLING §7/§14b) plus a per-node `url`. `descendants` follows
        `child` edges, `ancestors` follows `parent` edges; a visited set guards
        cousin-marriage cycles. Redaction is applied per node in `_tree_node`.
        `max_hops` is a safety net, not a display bound (#152 review fix, P2,
        finding 1) - see `_DESCENDANT_TREE_MAX_HOPS`'s comment. Hitting it
        drops real genealogy data, so callers pass a bound generous enough
        that no real archive should ever reach it, and a hit is warned about
        below rather than silently accepted."""
        rel = 'parent' if mode == 'ancestors' else 'child'
        order = [seed]
        seen = {seed}
        edges: list[dict] = []
        truncated = False
        queue: deque[tuple[str, int]] = deque([(seed, 0)])
        while queue:
            cur, hop = queue.popleft()
            if max_hops is not None and hop >= max_hops:
                truncated = True
                continue
            for r in self._tree_relationship_rows(cur, rel):
                other = r['other_id']
                if not self.linked:
                    # Include a deceased person even when they have no page of their
                    # own (a `stub`): they render as an unlinked name-only node, so
                    # the lineage isn't severed at every un-curated ancestor. Only a
                    # living/unknown/restricted person is dropped outright (never a
                    # standalone tree node), plus a relationship with no public claim.
                    ometa = self.person_meta.get(other)
                    if ometa is None or self._person_is_redacted(ometa):
                        continue
                    if not self._has_public_claim(cur, other):
                        continue
                # The edge's nature (SPEC §12.2): a non-genetic parent/child bond
                # (adoptive, step, foster, guardian, …) draws distinctly from the
                # genetic line. Unset/legacy subtypes default to genetic.
                subtype = (r['subtype'] or '').strip().lower() or None
                genetic = is_genetic_parent_subtype(subtype)
                edges.append({
                    'type': rel, 'from': fmt_id_display(cur), 'to': fmt_id_display(other),
                    'claim_id': fmt_id_display(r['claim_id']) if r['claim_id'] else None,
                    'subtype': subtype,
                    'genetic': genetic,
                    # Edge kind for the renderer (SPEC §12.2): 'genetic', or 'legal'
                    # for a non-genetic parent/child bond (adoptive/step/foster/
                    # guardian). Lateral 'other' ties (friend/associate/neighbor)
                    # are not parent/child edges and never enter the tree. `genetic`
                    # is kept for back-compat with the neutral-JSON contract.
                    'kind': 'genetic' if genetic else 'legal',
                    'dates': {'start': None, 'end': None},
                })
                if other not in seen:
                    seen.add(other)
                    order.append(other)
                    queue.append((other, hop + 1))
        if truncated:
            # Should not happen in any real archive (see `_DESCENDANT_TREE_
            # MAX_HOPS`'s comment) - surfaced loudly rather than silently
            # dropping generations from the reusable JSON artifact.
            self.messages.append(
                f'WARNING: {mode} tree for {fmt_id_display(seed)} exceeded '
                f'{max_hops} generations and was truncated; some real '
                f'genealogy data was omitted from its tree JSON.')
        return {
            'seed': fmt_id_display(seed), 'mode': mode,
            'nodes': [self._tree_node(pid, page_dir) for pid in order],
            'edges': edges,
        }

    def _chart_entry(self, pid: str, page_dir: Path) -> dict:
        """One redacted display node {'name','url','redacted','dates'} for any
        static chart (pedigree ancestors, and - as of the family-chart win -
        spouses/children too). Shared so every chart node gets identical
        redaction treatment (mirrors `_tree_node`, the interactive-tree
        equivalent): a living/restricted person redacts to a blank name, a
        stub (no meta row) shows its bare id unlinked, everyone else gets
        their real name plus a link when they have a page."""
        no_dates = {'birth': None, 'death': None}
        meta = self.person_meta.get(pid)
        if meta is None:
            return {'name': fmt_id_display(pid), 'url': None, 'redacted': False, 'dates': no_dates}
        if not self.linked and self._person_is_redacted(meta):
            return {'name': '', 'url': None, 'redacted': True, 'dates': no_dates}
        url = (_rel_href(self.persons_dir / _page_filename(pid), page_dir)
               if pid in self.person_pages else None)
        # Dates ride along for the pedigree card; the radial fan ignores them.
        return {'name': meta['name'] or fmt_id_display(pid), 'url': url,
                'redacted': False, 'dates': self._person_vitals(pid)}

    def _build_ahnentafel(self, seed: str, max_gen: int, page_dir: Path) -> tuple[dict, dict]:
        """Ahnentafel map {number: {'name','url','redacted', 'sex_derived'}} for
        the fan chart, walking `parent` edges from the seed, plus a second map
        {number: pid} of each EMPTY ancestor slot's known child (workbench mode
        wires this onto the pedigree's 'Unknown' placeholder so it opens 'add
        family' scoped to that child - the slot that is actually missing a
        parent, not a fixed subject). Father (a parent recorded M) takes the
        even slot, mother (F) the odd one; unknown-sex parents fill whatever
        slot is free. Redaction is applied per person - a withheld ancestor
        becomes a blank segment, never a leaked name (mirrors `_tree_node`).

        GENETIC LINE ONLY (#152 review fix, P2, SPEC §12.2). The Ahnentafel
        numbering - and the branch coloring it drives - means "the genetic
        pedigree": a parent edge whose claim carries an explicit non-genetic
        `subtype` (adoptive/step/foster/guardian/surrogate-gestational/social)
        never occupies a slot here, so a social/legal parent (and their whole
        ancestor line behind them) is never falsely presented as biological
        ancestry. An unset/legacy/unrecognised subtype defaults to genetic
        (`is_genetic_parent_subtype`, back-compat, SPEC §12.2) - the person/
        relationship views already draw a non-genetic child distinctly
        (bracket labels, W127); this is that same distinction applied to
        pedigree eligibility rather than to display styling, per the review's
        own steer ("filtering out is simpler and safer"). Decided per OTHER_ID
        across every claim behind that edge, not per row: the schema does not
        actually forbid two separate claims about the same parent-child pair
        carrying different subtypes, so one genuinely genetic claim is enough
        to seat the parent even if some other claim about the same pair
        happens to carry a social subtype too.

        SLOT-ASSIGNMENT PROVENANCE (#152 review fix, P2). Each filled slot's
        label also carries `sex_derived`: True when the occupant was matched
        by an explicit recorded sex (father via `sex: M`, mother via `sex: F`),
        False when it was placed by elimination - the only candidate(s) left
        after the sex match, e.g. two parents who are both unknown-sex, or an
        unknown-sex parent paired with a same-sex-recorded co-parent. Only the
        SEED's own two immediate parents (slots 2/3) actually affect anything
        downstream: `_ancestor_branch` reduces every deeper slot back to 2 or
        3 by halving, so a slot-4-vs-5 (etc) elimination never crosses the
        paternal/maternal boundary - but this is tracked uniformly at every
        generation rather than special-cased to slot 2/3, both because it
        costs nothing extra here and so the field stays meaningful if a future
        caller ever wants it deeper. `_render_pedigree_svg` reads it (via the
        labels dict) to withhold branch coloring from a slot whose parity was
        never actually evidenced by a `sex:` value."""
        labels: dict[int, dict] = {1: self._chart_entry(seed, page_dir)}
        missing_parent_of: dict[int, str] = {}
        queue: deque[tuple[int, str]] = deque([(1, seed)])
        seen = {seed}
        while queue:
            num, pid = queue.popleft()
            if num.bit_length() - 1 >= max_gen:
                continue
            # other_id -> {'sex', 'genetic'}: aggregated across every claim
            # backing that one edge, since adding claim_id/subtype to the
            # SELECT means DISTINCT no longer collapses multiple claims about
            # the same pair into one row the way the old sex-only query did.
            parent_rows: dict[str, dict] = {}
            for r in self.conn.execute(
                '''SELECT DISTINCT r.other_id, p.sex, r.claim_id, c.subtype,
                          c.source_id, c.status
                   FROM relationships r JOIN persons p ON r.other_id = p.id
                   LEFT JOIN claims c ON r.claim_id = c.id
                   WHERE r.person_id = ? AND r.rel = 'parent' ''', (pid,)):
                other = r['other_id']
                # A deceased ancestor without a page (stub) still fills its slot as a
                # name (unlinked); only a living/unknown/restricted ancestor stays a
                # blank redaction, and a no-public-claim edge is skipped.
                ometa = self.person_meta.get(other)
                if not self.linked and (ometa is None or self._person_is_redacted(ometa)
                                        or not self._has_public_claim(pid, other)):
                    continue
                subtype = (r['subtype'] or '').strip().lower() or None
                entry = parent_rows.setdefault(
                    other, {'sex': (r['sex'] or '').upper(), 'genetic': False})
                # P1 (Codex review, PR #152 round): a pair can carry BOTH a
                # public non-genetic claim (adoptive) and a restricted genetic
                # one - the pair-wide `_has_public_claim` check just above lets
                # `other` into the pedigree via the public adoptive claim, but
                # that must not also let THIS row's restricted genetic subtype
                # set the flag; only a row whose OWN claim is itself
                # publishable may mark the tie genetic. --linked shows every
                # edge, same as everywhere else on this page.
                if is_genetic_parent_subtype(subtype) and (
                        self.linked or self._claim_row_is_publishable(
                            r['claim_id'], r['source_id'], r['status'])):
                    entry['genetic'] = True
            parents: list[tuple[str, str]] = [
                (other, info['sex']) for other, info in parent_rows.items() if info['genetic']]
            # Workbench only: a parent recorded as a frontmatter hypothesis
            # (the add-family flow's whole output - never indexed) occupies
            # their slot too. Without this the slot still drew 'Unknown - add'
            # and a second click minted a duplicate parent stub (P2 codex
            # finding, round 3, PR #31). The card is tagged so the chart
            # never passes an unsourced belief off as a claim-backed edge;
            # standalone/plain-linked builds are untouched (unsourced ties
            # must not publish). Unsourced, so there is no claim to carry a
            # subtype - a hypothesis parent is always treated as genetic
            # (matches the "unset defaults to genetic" back-compat rule).
            hyp_parents: set[str] = set()
            if self.workbench:
                known = {p for p, _ in parents}
                for hp in self._hypothesis_parent_ids(pid):
                    if hp in known or hp in hyp_parents:
                        continue
                    hrow = self.person_meta.get(hp)
                    if hrow is None:
                        continue
                    hyp_parents.add(hp)
                    parents.append((hp, (hrow['sex'] or '').upper()))
            father = next((p for p, s in parents if s == 'M'), None)
            father_derived = father is not None
            mother = next((p for p, s in parents if s == 'F' and p != father), None)
            mother_derived = mother is not None
            rest = [p for p, s in parents if p not in (father, mother)]
            if father is None and rest:
                father = rest.pop(0)
            if mother is None and rest:
                mother = rest.pop(0)
            for slot_num, ppid, sex_derived in (
                    (2 * num, father, father_derived), (2 * num + 1, mother, mother_derived)):
                if not ppid:
                    missing_parent_of[slot_num] = pid
                    continue
                labels[slot_num] = self._chart_entry(ppid, page_dir)
                labels[slot_num]['sex_derived'] = sex_derived
                if ppid in hyp_parents:
                    labels[slot_num]['hypothesis'] = True
                if ppid not in seen:          # pedigree collapse: show, don't re-walk
                    seen.add(ppid)
                    queue.append((slot_num, ppid))
        return labels, missing_parent_of

    def _build_family_wings(self, pid: str, page_dir: Path, *,
                            include_siblings: bool = False) -> dict:
        """Spouse(s) and children for the person-page family chart (the win-1
        extension of the ancestor pedigree), as two lists of `_chart_entry`
        dicts, keyed 'spouses' / 'children'.

        `include_siblings` (#115, home pedigree hub only - never set by
        `build_person_page`) adds a third 'siblings' list, from `_hub_siblings`.
        Kept opt-in rather than always-on so an ordinary person's own chart is
        byte-for-byte unchanged: the "lost aunts/uncles/cousins" gap this
        mitigates is specific to the home page's pedigree-only first screen,
        not a defect in a person's own chart (which already links every
        relative from Friends & Family).

        Unlike ancestor slots, a redacted spouse or child is not shown as a
        faint 'Unknown' placeholder - you cannot enumerate someone's unknown
        children the way an unresearched parent slot can be drawn, so the
        entry is dropped outright. This mirrors what already happens to a
        redacted ANCESTOR in practice: `_build_ahnentafel`'s walk excludes a
        living/restricted parent from `parents` before it ever reaches
        `_chart_entry`, so that slot renders as the ordinary empty-ancestor
        placeholder rather than a labelled redaction. Dropping the person here
        is the same outcome translated to a column with no placeholder to
        fall back on: the safest rendering is silence, not a 'Living Person'
        chip that would out them as an unnamed close relative.

        The gate is identical to the ancestor one: standalone mode requires a
        meta row, a non-redacted person, and at least one public (non-hard-
        restricted) claim behind the edge. `--linked` shows every edge.

        SUBJECT redaction (review fix, PR #152). Every check above protects
        the OTHER person in each edge; ordinarily that is the whole story,
        because `pid` here is always a page subject and a redacted person
        never gets a page (`person_pages` excludes them under standalone) -
        so `pid` itself was never redacted in practice, until #115 gave this
        function one caller where that assumption breaks: the home
        pedigree's hub-only fallback (`_build_home_pedigree`) can call this
        with `pid` = the still-living/redacted seed itself, when no eligible
        substitute ancestor was found. Showing that seed's real, dated,
        linked spouse(s)/children/siblings bracketed directly onto their own
        blank 'Living Person' card is the mirror image of the leak this
        function already guards against above - a named close relative
        wired to an otherwise-silent redacted person - so the same "dropped
        outright, no placeholder" rule applies to the SUBJECT side too: when
        `pid` is itself redacted (standalone only), every wing (including
        `_hub_siblings`, which this short-circuit also skips calling) comes
        back empty rather than naming who a living person's family is."""
        if not self.linked:
            pid_meta = self.person_meta.get(pid)
            if pid_meta is None or self._person_is_redacted(pid_meta):
                return ({'spouses': [], 'children': [], 'siblings': []} if include_siblings
                        else {'spouses': [], 'children': []})

        def collect(rel: str) -> list[dict]:
            # Spouses arrive marriage-date-first: the renderer draws the FIRST
            # spouse as the solid primary bracket and later ones dotted, so
            # "first" must mean the earliest marriage, not the luck of id
            # ordering. `date_start` is the marriage claim's date carried onto
            # the relationship edge; undated marriages sort after dated ones,
            # then by id for a stable draw order. Children keep plain id
            # order - their edges rarely carry dates and nothing downstream
            # reads meaning into their sequence.
            if rel == 'spouse':
                query = ('SELECT other_id FROM relationships '
                         'WHERE person_id = ? AND rel = ? GROUP BY other_id '
                         'ORDER BY MIN(date_start) IS NULL, MIN(date_start), other_id')
            else:
                query = ('SELECT DISTINCT other_id FROM relationships '
                         'WHERE person_id = ? AND rel = ? ORDER BY other_id')
            out: list[dict] = []
            for r in self.conn.execute(query, (pid, rel)):
                other = r['other_id']
                if other == pid:
                    continue
                if not self.linked:
                    ometa = self.person_meta.get(other)
                    if (ometa is None or self._person_is_redacted(ometa)
                            or not self._has_public_claim(pid, other)):
                        continue
                entry = self._chart_entry(other, page_dir)
                entry['id'] = other
                out.append(entry)
            return out

        spouses = collect('spouse')
        children = collect('child')
        # Workbench only: spouses/children recorded as frontmatter
        # `relationships:` hypotheses (the add-family flow's whole output,
        # never indexed) join the wings too, tagged exactly like the
        # hypothesis ancestor slots - the chart and the family strip must
        # agree about who is in the immediate family, and until this the
        # just-added spouse/child showed on the strip but the chart stayed
        # unchanged (P2 codex finding, round 5, PR #31). Appended after the
        # indexed edges: a hypothesis marriage has no date, so it belongs in
        # the undated tail anyway, and the renderer draws later spouses
        # dotted - the honest look for an unsourced tie. Standalone and
        # plain-linked builds never take this branch (unsourced ties do not
        # publish).
        if self.workbench:
            row = self.person_meta.get(pid)
            meta = None
            if row is not None:
                try:
                    rec = read_record(self.archive_root / row['path'],
                                       on_decode_error=_ignore_decode_error)
                except Exception:  # noqa: BLE001 - an unreadable record contributes nothing
                    rec = None
                # Same file `_person_prose` reads for this same page, earlier
                # in `build_person_page` - its own WARNING already names an
                # undecodable one, so contributing nothing here (rather than
                # repeating it) is deliberate, not an oversight.
                if rec is not None and not rec.get('undecodable'):
                    meta = rec['meta']
            if meta:
                have = {'spouses': {normalize_id(str(s['id'])) for s in spouses},
                        'children': {normalize_id(str(c['id'])) for c in children}}
                wing_of = {'spouses': spouses, 'children': children}
                for group, target in self._hypothesis_tie_ids_from_meta(meta, pid):
                    if group not in wing_of or target in have[group]:
                        continue
                    if self.person_meta.get(target) is None:
                        continue
                    entry = self._chart_entry(target, page_dir)
                    entry['id'] = target
                    entry['hypothesis'] = True
                    wing_of[group].append(entry)
                    have[group].add(target)
        # Each child's OTHER parent(s), so the renderer can hang the child off
        # the right couple's junction - a person with children by two spouses
        # draws two family brackets, each splitting to its own children. A
        # child whose co-parent is not among the drawn spouses (unrecorded, or
        # redacted - the spouse list already excludes redacted people) falls
        # back to hanging off the subject alone, which is also the privacy-safe
        # rendering: it never hints at who the withheld parent is.
        for ch in children:
            ch['co_parents'] = [
                r['other_id'] for r in self.conn.execute(
                    "SELECT DISTINCT other_id FROM relationships "
                    "WHERE person_id = ? AND rel = 'parent' "
                    "ORDER BY other_id", (ch['id'],))
                if r['other_id'] != pid
            ]
        result = {'spouses': spouses, 'children': children}
        if include_siblings:
            result['siblings'] = self._hub_siblings(pid, page_dir)
        return result

    def _hub_siblings(self, pid: str, page_dir: Path) -> list[dict]:
        """The home pedigree hub's own siblings (#115 lost-relatives
        mitigation): everyone else who shares at least one of the hub's own
        recorded parents, found by re-querying the same `relationships` rows
        the ancestor walk already reads for parent edges - no new edge type,
        no dedicated 'sibling' relationship needed (none is derived by the
        indexer - see `_FAMILY_GROUPS`'s comment). A half-sibling (one shared
        parent) counts the same as a full sibling: genealogically both are
        siblings, and the alternative (silently dropping half-siblings) has
        no basis in the design decision, which just says 'the same two
        parents' loosely.

        Gated like an ancestor slot, not like a spouse/child: standalone mode
        drops a candidate who is redacted (living/unknown/restricted), OR
        whose OWN parent-child claim (linking them to the shared parent) is
        hard-restricted - the same `_has_public_claim` check `_build_ahnentafel`
        runs for a parent slot, applied here from the shared parent's side
        since hub and sibling rarely share a claim naming them TOGETHER (each
        child's birth/baptism is usually its own separate record). Dropped
        outright, never a placeholder - like a redacted spouse/child, you
        cannot show 'a sibling exists' without showing who.

        A candidate is gated on its BEST tie, not its first-visited one
        (#152 review fix): every shared-parent link is collected before any
        inclusion decision is made, so a full sibling (sharing BOTH of the
        hub's parents) whose tie to the first-visited shared parent happens
        to be non-public still qualifies via the OTHER shared parent's tie,
        if that one is public. The earlier version marked a candidate `seen`
        the moment it was first reached and never revisited that decision,
        so the exact same full sibling was silently excluded whenever the
        two parent ids happened to sort with the non-public tie first -
        under-inclusion only (never a privacy leak: a candidate still needs
        at least one public tie and a clean redaction check to appear at
        all), but a real completeness bug.

        A parent must be one the HUB is itself PUBLICLY tied to before it
        contributes any sibling candidate at all (#152 review fix, P1,
        privacy-adjacent). The candidate-side check below (a sibling's own
        tie to the shared parent) is not enough on its own: it protects the
        CANDIDATE's privacy, but says nothing about the HUB's. If the hub's
        own parent-child claim to a parent is restricted (a sealed/DNA-only
        source, say) while that SAME parent has a perfectly public tie to
        some other child, the old code still walked that parent's children
        and published the other child as the hub's sibling - which tells a
        reader "the hub is tied to this parent" exactly as surely as
        printing the parent's name on the hub's own card would, defeating
        the restriction's whole point. A parent the hub is not publicly
        tied to is therefore dropped BEFORE it is ever used as a source of
        candidates - in standalone mode; `--linked` shows every edge, same
        as everywhere else on this page."""
        parent_ids = [r['other_id'] for r in self.conn.execute(
            "SELECT DISTINCT other_id FROM relationships WHERE person_id = ? AND rel = 'parent'",
            (pid,))]
        if not self.linked:
            # P1 (Codex review, PR #152 round): `_has_public_claim` asks "is
            # ANY claim connecting these two people public, about anything" -
            # an unrelated public claim (a shared census entry, a residence
            # record) could satisfy it even while the hub's OWN parent-child
            # tie to `p` is backed only by a restricted claim, defeating the
            # whole point of gating siblings on the hub's own public tie.
            # `_has_public_parent_edge` asks the narrower, correct question:
            # is the parent-child relationship itself publicly backed.
            parent_ids = [p for p in parent_ids if self._has_public_parent_edge(pid, p)]
        # other_id -> every shared parent_id tying them to the hub, in
        # first-discovered order (a plain dict preserves insertion order) -
        # collected in full before any candidate is gated, so a later
        # parent's public tie can still qualify someone an earlier parent's
        # non-public tie alone would have excluded.
        ties: dict[str, list[str]] = {}
        for parent_id in sorted(parent_ids):
            for r in self.conn.execute(
                    "SELECT DISTINCT other_id FROM relationships "
                    "WHERE person_id = ? AND rel = 'child' ORDER BY other_id", (parent_id,)):
                other = r['other_id']
                if other == pid:
                    continue
                ties.setdefault(other, []).append(parent_id)

        out: list[dict] = []
        for other, via_parents in ties.items():
            if not self.linked:
                ometa = self.person_meta.get(other)
                if ometa is None or self._person_is_redacted(ometa):
                    continue
                if not any(self._has_public_claim(par, other) for par in via_parents):
                    continue
            entry = self._chart_entry(other, page_dir)
            entry['id'] = other
            out.append(entry)
        return out

    def _make_tree_ctx(self, seed: str, mode: str, max_hops: int | None,
                       page_dir: Path, caption: str, *, initial_depth: int | None = None,
                       home_id: str | None = None) -> dict | None:
        """Build a tree, write its `data/tree_{seed}_{mode}.json` artifact, and
        return the template context (inline-embeddable JSON + caption). Returns
        None when the tree has no edges (a lone node is not worth rendering), so
        the page simply omits the tree section. `initial_depth` bounds the
        renderer's initial paint (deeper nodes start collapsed) for potentially
        large descendant explorers; None shows every generation expanded."""
        tree = self._build_tree_data(seed, mode, max_hops, page_dir)
        if not tree['edges']:
            return None
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / f'tree_{normalize_id(seed)}_{mode}.json').write_text(
                json.dumps(tree, indent=2, ensure_ascii=False), encoding='utf-8')
        except OSError as e:
            self.messages.append(f'WARNING: could not write tree data for {fmt_id_display(seed)} ({e}).')
        return {'data_json': self._markup(_json_for_script(tree)), 'caption': caption,
                'initial_depth': initial_depth,
                'home_id': fmt_id_display(home_id) if home_id else None}

    def _copy_vendor(self) -> None:
        """Copy the vendored tree renderer + adapter into the site so it stays
        self-contained and offline (no CDN). The bundle lives beside the
        templates; a missing bundle is a packaging error, surfaced plainly."""
        src = Path(__file__).parent / 'templates' / 'vendor'
        if not src.is_dir():
            self.messages.append('WARNING: tools/templates/vendor is missing; interactive trees will not load.')
            return
        try:
            shutil.copytree(src, self.vendor_dir, dirs_exist_ok=True)
        except OSError as e:
            self.messages.append(f'WARNING: could not copy the tree library into the site ({e}).')

    def _copy_assets(self) -> None:
        """Copy the design system - stylesheet, user override, self-hosted fonts -
        into the site so it is self-contained and offline (no CDN). The canonical
        source is the repo-root `design/` package (see docs/DESIGN.md); a missing
        package is a packaging error, surfaced plainly rather than silently
        leaving every page unstyled."""
        src = Path(__file__).resolve().parent.parent / 'design'
        if not src.is_dir():
            self.messages.append('WARNING: design/ package is missing; generated pages will be unstyled.')
            return
        try:
            self.assets_dir.mkdir(parents=True, exist_ok=True)
            for name in ('styles.css', 'custom.css'):
                f = src / name
                if f.is_file():
                    shutil.copy2(f, self.assets_dir / name)
            # Pages always link custom.css; write an empty stub if the source has
            # none so the link never 404s.
            if not (self.assets_dir / 'custom.css').is_file():
                (self.assets_dir / 'custom.css').write_text(
                    '/* Local overrides - linked after styles.css. See docs/DESIGN.md. */\n',
                    encoding='utf-8')
            fonts = src / 'fonts'
            if fonts.is_dir():
                shutil.copytree(fonts, self.assets_dir / 'fonts', dirs_exist_ok=True)
            # Workbench mode ships the serve chrome's own stylesheet + script
            # into assets/ so the served pages (built here, plus serve's own
            # /review and /inbox which reference the same assets/ dir) stay
            # self-contained under the snapshot root. These files never exist in
            # a standalone/linked build - they are only copied when workbench.
            if self.workbench:
                wb_src = Path(__file__).resolve().parent / 'templates' / 'workbench'
                for name in ('workbench.css', 'workbench.js'):
                    f = wb_src / name
                    if f.is_file():
                        shutil.copy2(f, self.assets_dir / name)
        except OSError as e:
            self.messages.append(f'WARNING: could not copy the design assets into the site ({e}).')

    # - home pedigree (#115) -

    def _home_pedigree_depth(self, site_cfg: dict) -> int:
        """`site.home_pedigree_generations` (#115), defaulting to
        `_HOME_PEDIGREE_GENERATIONS_DEFAULT` (5) and clamped to
        `_HOME_PEDIGREE_GENERATIONS_MAX` - "depth control, not an infinite
        canvas" is the decided design: each extra generation DOUBLES the
        ancestor-slot count (2**(N+1) slots total), so an unclamped typo like
        `50` would try to lay out over a quadrillion slots. A malformed or
        out-of-range value degrades to the nearest sane default with a
        warning rather than failing the whole build - the home page is worth
        finishing even when one setting is wrong.

        Genuinely-a-whole-number check (#152 review fix, P2): `int(raw)`
        alone is not that check - it silently TRUNCATES a float instead of
        raising (`int(3.9) == 3`), so a fractional YAML value degraded the
        chart a generation or more shallower than configured with no warning
        at all, and it accepts a YAML boolean as `1`/`0` (`bool` is an `int`
        subclass in Python) even though a boolean was plainly never meant as
        a generation count. A `bool` is rejected outright (never a generation
        count, whatever its int value), and a genuinely fractional or
        non-finite float (`3.5`, `.nan`, `.inf`) takes the warned default
        fallback the same way a non-numeric string already does.

        A FLOAT WHOSE VALUE IS WHOLE (`3.0`) is different (#152 follow-up
        review fix, P2, finding 6): PyYAML parses a hand-edited
        `home_pedigree_generations: 3.0` as a Python `float`, not an `int` -
        an easy, harmless slip for a human editing YAML by hand, and
        mathematically an actual whole number the reader plainly meant as
        one. Silently substituting the default depth (5) instead of the
        requested 3 is not the recoverable-loose-input behavior this
        fallback exists for elsewhere in this method (a bad string, an
        out-of-range int) - so a finite float that survives an integer round
        trip (`int(raw) == raw`) is coerced to that `int` and accepted like
        any other whole number, no warning needed since nothing was actually
        wrong with what the human wrote."""
        raw = site_cfg.get('home_pedigree_generations')
        if raw is None:
            return _HOME_PEDIGREE_GENERATIONS_DEFAULT
        if isinstance(raw, bool):
            self.messages.append(
                f'WARNING: fha.yaml site.home_pedigree_generations {raw!r} is not a whole number; '
                f'using the default of {_HOME_PEDIGREE_GENERATIONS_DEFAULT}.')
            return _HOME_PEDIGREE_GENERATIONS_DEFAULT
        if isinstance(raw, float):
            if not math.isfinite(raw) or raw != int(raw):
                self.messages.append(
                    f'WARNING: fha.yaml site.home_pedigree_generations {raw!r} is not a whole '
                    f'number; using the default of {_HOME_PEDIGREE_GENERATIONS_DEFAULT}.')
                return _HOME_PEDIGREE_GENERATIONS_DEFAULT
            raw = int(raw)   # 3.0 -> 3: a whole number, just spelled as a float
        try:
            n = int(raw)
        except (TypeError, ValueError):
            self.messages.append(
                f'WARNING: fha.yaml site.home_pedigree_generations {raw!r} is not a whole number; '
                f'using the default of {_HOME_PEDIGREE_GENERATIONS_DEFAULT}.')
            return _HOME_PEDIGREE_GENERATIONS_DEFAULT
        if n < 1:
            self.messages.append(
                'WARNING: fha.yaml site.home_pedigree_generations must be at least 1; using the '
                f'default of {_HOME_PEDIGREE_GENERATIONS_DEFAULT}.')
            return _HOME_PEDIGREE_GENERATIONS_DEFAULT
        if n > _HOME_PEDIGREE_GENERATIONS_MAX:
            self.messages.append(
                f'WARNING: fha.yaml site.home_pedigree_generations {n} is above the maximum of '
                f'{_HOME_PEDIGREE_GENERATIONS_MAX}; using {_HOME_PEDIGREE_GENERATIONS_MAX}.')
            return _HOME_PEDIGREE_GENERATIONS_MAX
        return n

    def _build_home_pedigree(self, seed: str, page_dir: Path, site_cfg: dict) -> dict:
        """Build the home page's marriage-aware ancestor pedigree (#115): the
        static SVG that replaced the descendant explorer as the home page's
        centrepiece. `seed` is the already-validated configured hub
        (`site.home_person`, falling back to `root_person` - resolved and
        checked against the index by the caller, `build_index_page`); this
        method's own job is deciding whether that seed is safe to actually
        SHOW as the hub and, when it is not, finding one that is.

        REDACTION-SAFE HUB (standalone only). `seed` is typically the living
        archive owner - a standalone/public build must not open on a hub card
        reading blank ('Living Person') with an entire chart hanging off it.
        A non-redacted seed is used unchanged. A redacted seed hands off to
        `_apex_ancestor` (repurposed - see its own docstring) to find the
        CLOSEST eligible ancestor on the recorded line; when even that finds
        nobody at all, the fallback is a blank hub-only render - just
        `seed`'s own blank card, NO ancestor columns (`ancestor_generations=0`)
        AND no spouse/children/siblings row either, plus a plain-language
        `note` the template shows instead of a chart, rather than a page
        mostly built of blank/'Unknown' cards.

        Review fix (PR #152): this last case used to still draw `seed`'s own
        family row - real, dated, linked spouse(s)/children/siblings
        bracketed onto that same blank card - which outs a living person's
        specific close relatives on the single highest-traffic page of the
        site, the mirror image of the leak `_build_family_wings` already
        guards against for every OTHER redacted person. `_build_family_wings`
        now withholds all three lists outright (same function, same call
        below) whenever its own subject is itself redacted, which in
        practice is only ever true in this one fallback branch - see its
        docstring. `--linked`/workbench never substitutes - the local
        preview always seeds on the real configured person, and never hits
        this branch's redaction checks at all.

        Returns the index.html template context: {'svg', 'caption', 'note'}.
        `note` is None on every ordinary build; it is set only in that last,
        rare case."""
        hub = seed
        note: str | None = None
        if not self.linked:
            meta = self.person_meta.get(seed)
            if meta is None or self._person_is_redacted(meta):
                found = self._apex_ancestor(seed)
                if found is not None:
                    hub = found
                else:
                    hub = seed
                    note = ("No ancestor eligible to publish was found on this person's recorded "
                            "line, and their own spouse, children, and siblings cannot be shown "
                            "here without naming a living person, so no pedigree is shown on the "
                            "home page.")

        hub_meta = self.person_meta.get(hub)
        if not self.linked and hub_meta and self._person_is_redacted(hub_meta):
            hub_name = _LIVING_LABEL
        elif hub_meta and hub_meta['name']:
            hub_name = hub_meta['name']
        else:
            hub_name = fmt_id_display(hub)

        depth = 0 if note else self._home_pedigree_depth(site_cfg)
        ahnen, missing_parent_of = self._build_ahnentafel(hub, depth, page_dir)
        wings = self._build_family_wings(hub, page_dir, include_siblings=True)
        # hub_name is always the blank _LIVING_LABEL here (`note` is only set
        # when `seed`/`hub` is itself redacted), so a caption built from it
        # ("Living Person's recorded family") would name nothing real - the
        # explanatory `note` below already carries the actual message.
        caption = ('No public pedigree is available for the home page.' if note else
                  f"{hub_name}'s family, tracing back through the generations →")
        axis_label = 'ancestors →' if depth else None
        svg = _render_pedigree_svg(
            ahnen, wings['spouses'], wings['children'], missing_parent_of=missing_parent_of,
            workbench=self.workbench, siblings=wings.get('siblings'),
            ancestor_generations=depth, branch_color=bool(depth), axis_label=axis_label, home=True)
        return {'svg': self._markup(svg), 'caption': caption, 'note': note}

    # - index / home page (M8.4) -

    def build_index_page(self) -> None:
        """The home page (TOOLING §12 / M8.4): a surname A-Z index of people and
        a recent-discoveries teaser (last five entries), plus source and place
        navigation so every generated page is reachable. The surname index is
        built from `person_pages`, which already excludes redacted persons under
        standalone - so the home page never lists or links a living person."""
        page_dir = self.out_dir
        render = lambda tok, disp=None: self.render_token(tok, page_dir, disp)  # noqa: E731
        embed = lambda t, c: self._render_embed(t, c, page_dir)  # noqa: E731

        # Surname A-Z: group people by surname initial. In the workbench every
        # recorded person is in person_pages (stubs included - owner decision,
        # live review 2026-07-16), so a stub lists inline in its surname group
        # as a linked entry marked stub (its page carries the open-file escape
        # hatch; the list itself stays clean - owner request, 2026-07-16).
        # Published/standalone output stays curated-only (person_pages already
        # excludes stubs there).
        by_letter: dict[str, list[dict]] = {}
        for pid in self.person_pages:
            meta = self.person_meta[pid]
            name = meta['name'] or fmt_id_display(pid)
            surname = (meta['surname'] or name or '?').strip()
            letter = surname[:1].upper() if surname[:1].isalpha() else '#'
            by_letter.setdefault(letter, []).append(
                {'name': name, 'href': f'persons/{_page_filename(pid)}',
                 'stub': (meta['tier'] or '') == 'stub'})
        if self.workbench:
            # A person named in claims with NO record at all (lint's E005 set)
            # is otherwise invisible: list them under '#' with the wireframe's
            # mint '+' so one click creates their stub REUSING the claim's
            # P-id (person.new accepts person_id), keeping every claim that
            # names them pointing at the same person.
            recordless = [
                r['person_id'] for r in self.conn.execute(
                    'SELECT DISTINCT person_id FROM claim_persons')
                if r['person_id'] not in self.person_meta
            ]
            for pid in sorted(recordless):
                by_letter.setdefault('#', []).append(
                    {'name': fmt_id_display(pid), 'href': None, 'stub': True,
                     'recordless': True, 'person_id': fmt_id_display(pid)})
        surnames = [
            {'letter': letter, 'people': sorted(by_letter[letter], key=lambda p: p['name'].lower())}
            for letter in sorted(by_letter)
        ]

        _body, entries = self._read_discoveries()
        discoveries = [self._markup(_prose_to_html(chunk, render, embed, drop_private=not self.linked))
                       for chunk in entries]

        # Sources, grouped by the decade of the record's own date (a census
        # year, a letter's postmark - `date_min` is the EDTF lower bound the
        # index already computed) so a growing list stays scannable: decades
        # in order, alphabetical within each, undated records last under
        # their own heading (owner request, review 2026-07-16).
        by_decade: dict[str, list[dict]] = {}
        for sid in self.source_pages:
            row = self.source_meta[sid]
            year = (row['date_min'] or '')[:4]
            label = f'{int(year) // 10 * 10}s' if year.isdigit() else 'Undated'
            by_decade.setdefault(label, []).append(
                {'title': row['title'] or fmt_id_display(sid),
                 'href': f'sources/{_page_filename(sid)}'})
        source_groups = [
            {'label': label,
             'sources': sorted(by_decade[label], key=lambda s: s['title'].lower())}
            for label in sorted(by_decade, key=lambda g: (g == 'Undated', g))
        ]
        places = sorted(
            ({'name': self.place_meta[lid]['name'] or fmt_id_display(lid),
              'href': f'places/{_page_filename(lid)}'} for lid in self.place_pages),
            key=lambda p: p['name'].lower())
        # Homepage intro: notes/home.md (markdown, human + AI curated) when present,
        # else a default line. Its [[links]] and ![[S-id|caption]] embeds resolve
        # exactly as in any prose (and redact under standalone).
        default_intro = ('A safe-to-share snapshot of this family archive.' if not self.linked
                         else 'Local developer preview (linked mode - not redacted, do not share).')
        intro = self._markup(f'<p>{_escape(default_intro)}</p>')
        # The raw text `home.edit` would overwrite - workbench-only, so the
        # "Edit the homepage intro" replacement editor can prefill with what
        # is actually there instead of starting blank (a whole-section
        # REPLACE that started empty would delete the existing intro).
        intro_raw = ''
        home_md = self.archive_root / 'notes' / 'home.md'
        if home_md.is_file():
            try:
                body = (read_record(home_md,
                                     on_decode_error=_raise_friendly_decode_error)
                        .get('body') or '').strip()
                if body:
                    # The text AS WRITTEN - any pending AI-DRAFT block intact -
                    # captured before draft-stripping below, for the workbench
                    # editor prefill (`home.edit`'s whole-file REPLACE target).
                    # Reusing the stripped `body` here would silently delete a
                    # pending draft the moment any small homepage edit was
                    # applied, bypassing `fha confirm draft` entirely (P2
                    # codex finding, round 7, PR #30 - the same fix already
                    # applied to the person Biography editor in round 5).
                    body_as_written = body
                    # Fail-closed on `<!-- AI-DRAFT ... -->`: unaccepted drafts
                    # must not slip into the homepage prose, and a damaged marker
                    # withholds the whole intro rather than leak partial draft.
                    body, problem = strip_unaccepted_drafts(body)
                    if problem is not None:
                        self.messages.append(
                            'WARNING: a draft marker in notes/home.md is damaged '
                            f'({problem}) - the homepage intro is withheld until it is fixed.')
                    elif body.strip():
                        intro = self._markup(_prose_to_html(body, render, embed, drop_private=not self.linked))
                        if self.workbench:
                            intro_raw = body_as_written
            except Exception as e:  # noqa: BLE001 - a bad home.md just falls back to the default
                self.messages.append(
                    f'WARNING: notes/home.md could not be read ({e}); using the default intro.')

        # Optional hero banner. `fha.yaml site: hero:` is either a scalar photo
        # ref (an S-id or path - legacy shape) or a mapping documented in
        # CUSTOMIZING_SITE.md as `{image, title, tagline}`. Missing/unresolved →
        # the template shows a default patterned band.
        site_cfg = self.fha_config.get('site') if isinstance(self.fha_config.get('site'), dict) else {}
        hero: dict | None = None
        hero_cfg = site_cfg.get('hero')
        if isinstance(hero_cfg, dict):
            hero_ref = str(hero_cfg.get('image') or '').strip()
            hero_title = str(hero_cfg.get('title') or '').strip() or None
            hero_tagline = str(hero_cfg.get('tagline') or '').strip() or None
        else:
            hero_ref = str(hero_cfg or '').strip()
            hero_title = None
            hero_tagline = None
        hero_image = None
        if hero_ref:
            hero_image = self._image_href(hero_ref, page_dir, 'hero')
            if not hero_image:
                self.messages.append(
                    f'WARNING: site.hero {hero_ref!r} matched no publishable photo; using the default banner.')
        if hero_image or hero_title or hero_tagline:
            hero = {'image': hero_image, 'title': hero_title, 'tagline': hero_tagline}

        # Home pedigree (#115): the marriage-aware, static ancestor pedigree
        # that replaced the interactive descendant explorer as the home
        # page's centrepiece (that explorer is demoted, not deleted - see
        # `build_person_page`'s `descendants_tree`, the same
        # `_build_tree_data`/fha-tree.js/tree-adapter.js pipeline, now a
        # per-person opt-in link instead of the app's front door). Seeded on
        # `site.home_person`, falling back to `root_person` - the config key
        # `home_person` used to exist only to steer the OLD tree's 'Home'
        # button; it is now the pedigree's own seed, so a misconfigured value
        # here is worth its own warning rather than silently reverting to
        # root_person with no explanation.
        pedigree = None
        root_person = normalize_id(str(self.fha_config.get('root_person', '')))
        configured_home = normalize_id(str(site_cfg.get('home_person') or ''))
        root_valid = bool(root_person) and root_person in self.person_meta
        home_valid = bool(configured_home) and configured_home in self.person_meta

        if configured_home and not home_valid:
            self.messages.append(
                f"WARNING: fha.yaml site.home_person {fmt_id_display(configured_home)} is not in "
                "the index; falling back to root_person for the home pedigree. Check the id, or "
                "run `fha index` if it was just added."
            )
        # root_person's own problem is only worth naming when it is the thing
        # that actually cost the reader the pedigree - a broken root_person
        # behind a WORKING home_person never reaches the page at all, and a
        # warning claiming "the home pedigree was skipped" on a build that
        # built it fine (from home_person) would blame the wrong cause.
        if root_person and not root_valid and not home_valid:
            self.messages.append(
                f"WARNING: fha.yaml root_person {fmt_id_display(root_person)} is not in the index; "
                "the home pedigree was skipped. Check the id, or run `fha index` if it was just added."
            )

        hub_seed = configured_home if home_valid else (root_person if root_valid else '')
        if hub_seed:
            pedigree = self._build_home_pedigree(hub_seed, page_dir, site_cfg)

        self._write_page(self.out_dir / 'index.html', 'index.html', {
            'surnames': surnames, 'discoveries': discoveries, 'source_groups': source_groups,
            'places': places, 'intro': intro, 'intro_raw': intro_raw, 'pedigree': pedigree,
            'hero': hero, 'root_prefix': '.',
        })

    # - rendering plumbing -

    def _markup(self, raw_html: str):
        """Wrap pre-rendered HTML so Jinja's autoescape leaves it intact. The
        only un-escaped HTML reaching a template comes through here, and every
        such string was built by our own helpers (escaped at the leaves)."""
        return jinja2.utils.markupsafe.Markup(raw_html)

    def _write_page(self, path: Path, template: str, ctx: dict) -> None:
        """Render `template` with shared context and write it. Per-page failures
        are caught and reported so one bad page never aborts the whole build."""
        try:
            tmpl = self.env.get_template(template)
            site_cfg = self.fha_config.get('site') if isinstance(self.fha_config.get('site'), dict) else {}
            full = {
                # Customizable in fha.yaml under `site: archive_name:` (with a legacy
                # top-level `archive_name:` fallback); else the default.
                'site_title': (str(site_cfg.get('archive_name')
                                   or self.fha_config.get('archive_name') or '').strip()
                               or 'Family History Archive'),
                'footer_note': (
                    'Generated by fha site. Living people and restricted material are excluded from this snapshot.'
                    if not self.linked else
                    'Generated by fha site (linked preview - unredacted; do not publish).'
                ),
            }
            # Workbench chrome (serve only). base.html gates the serve bar, the
            # CSRF meta tag, the workbench assets, and the modal templates on
            # `workbench`; the runtime values it needs (port, per-process CSRF
            # token, review/inbox counts) are supplied by serve as
            # workbench_context. Both stay absent (falsy) in every `fha site`
            # build, so no chrome can leak into a shared snapshot.
            if self.workbench:
                full['workbench'] = True
                # Which vitals get a provisional (unsourced) slot, computed once
                # from _lib.PROVISIONAL_VITAL_FIELDS and handed to workbench.js
                # via a meta tag - the client-side milestone router reads this
                # instead of hardcoding its own birth/death literal, so the two
                # halves of the milestone feature cannot drift apart.
                full['provisional_vital_fields'] = ' '.join(sorted(PROVISIONAL_VITAL_FIELDS))
                full.update(self.workbench_context)
            full.update(ctx)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tmpl.render(**full), encoding='utf-8')
        except Exception as e:  # noqa: BLE001 - one page's failure must not abort the build
            self.messages.append(f'WARNING: could not generate {path.name} ({e}); skipped.')

    def run(self) -> int:
        """Generate the whole site. Returns the number of pages written."""
        self._reset_output()
        # Stamp ownership the moment _reset_output succeeds: the tool owns the
        # dir it just cleared/created, and an interrupted FIRST build must not
        # lock its own output (a crash mid-build used to leave a non-empty,
        # unmarked folder that the next run refused as not-ours). The marker
        # is written again on completion so its Last-build date is the
        # finished build's, not the aborted attempt's.
        self._write_marker()
        self._copy_vendor()
        self._copy_assets()
        for sid in sorted(self.source_pages):
            self.build_source_page(sid)
        for pid in sorted(self.person_pages):
            self.build_person_page(pid)
        for lid in sorted(self.place_pages):
            self.build_place_page(lid)
        self.build_discoveries_page()
        self.build_index_page()
        self._write_marker()
        # source + person + place pages, plus discoveries.html and index.html
        return len(self.source_pages) + len(self.person_pages) + len(self.place_pages) + 2

    def _reset_targets(self) -> list[Path]:
        """The output paths a rebuild clears - the one list `_reset_output`
        deletes from and `reset_preview` reports, so preview and deletion can
        never drift apart."""
        return [self.persons_dir, self.sources_dir, self.places_dir,
                self.media_dir, self.data_dir, self.vendor_dir, self.assets_dir,
                self.out_dir / 'index.html', self.out_dir / 'discoveries.html']

    def reset_preview(self) -> list[str]:
        """Names of the existing files/subtrees a rebuild would first remove,
        relative to the output dir - the `--dry-run` would-remove report."""
        names: list[str] = []
        for t in self._reset_targets():
            if t.is_dir():
                names.append(t.name + '/')
            elif t.exists():
                names.append(t.name)
        return names

    def _write_marker(self) -> None:
        """Stamp the output dir as fha-site-owned, so the next rebuild knows it
        may clear this folder (`_unowned_output_reason` checks for it).

        Called twice per build: right after `_reset_output` (so a crash or
        Ctrl-C mid-build cannot leave a partial site the next run refuses to
        rebuild) and again at completion (refreshing the Last-build date).

        A write failure is a warning, not a failed build: the finished site is
        valid either way, and the pre-marker back-compat rule (index.html +
        vendor/fha-tree.js) will still recognize the folder next time."""
        marker = self.out_dir / _SITE_MARKER_NAME
        try:
            marker.write_text(
                'This folder was generated by `fha site`.\n'
                'A rebuild clears and rewrites the site files in here - '
                'keep your own files somewhere else.\n'
                f'Last build: {_today()}\n',
                encoding='utf-8',
            )
        except OSError as e:
            self.messages.append(
                f'WARNING: could not write the {_SITE_MARKER_NAME} marker file ({e}); '
                'the next rebuild may ask you to point --out at a new or empty folder.'
            )

    def _reset_output(self) -> None:
        """Clear only the subtrees this tool owns, so a rebuild drops pages for
        records that became redacted (idempotent regeneration - TOOLING §12)
        without disturbing anything else a human keeps in the output directory.

        Ownership of the output dir itself was already verified upstream
        (`_unowned_output_reason` in `_site_payload`) before any build reaches
        this point, so a non-empty folder that was never an fha-site build is
        refused rather than cleared.

        Standalone builds raise OSError if a subtree cannot be removed - leaving a
        previously generated page for a now-redacted person would be a privacy leak.
        Linked (dev preview) mode silently ignores removal failures."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for target in self._reset_targets():
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=self.linked)
            elif target.exists():
                target.unlink()


# ── Core / CLI ──────────────────────────────────────────────────────────────

def _site_payload(
    archive_root: Path,
    out_dir: Path,
    *,
    linked: bool = False,
    dry_run: bool = False,
    workbench: bool = False,
    workbench_context: dict | None = None,
) -> dict:
    """Build the site and return a result dict.

    Returns {'status', 'messages', 'out_dir', 'pages'} where status is one of:
      'no-jinja'    - Jinja2 not installed (CLI prints an install hint)
      'no-index'    - index absent/unreadable/stale (open_index_db already explained;
                      standalone builds refuse a stale index - run `fha index` first)
      'bad-config'  - fha.yaml is malformed (message carries the detail)
      'bad-output'  - output dir would clobber archive content, or is a non-empty
                      folder fha site never built into (no .fha-site marker) -
                      the message names the fix in both cases
      'dry-run'     - would build N pages; nothing written. Carries an extra
                      'reset_preview' key: the existing files/subtrees a real
                      rebuild would first remove from the output dir
      'ok'          - built; 'messages' non-empty means finished with warnings
    """
    if jinja2 is None:
        return {'status': 'no-jinja', 'messages': [], 'out_dir': out_dir, 'pages': 0}

    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as exc:
        return {'status': 'bad-config', 'messages': [str(exc)], 'out_dir': out_dir, 'pages': 0}

    roots = fha_config.get('roots', {})
    if not isinstance(roots, dict):
        return {'status': 'bad-config', 'messages': [
            'fha.yaml: `roots` must be a mapping of alias: path pairs'
        ], 'out_dir': out_dir, 'pages': 0}
    for _alias, _val in roots.items():
        if not isinstance(_val, str):
            return {'status': 'bad-config', 'messages': [
                f'fha.yaml: roots.{_alias} must be a string path, got {type(_val).__name__}'
            ], 'out_dir': out_dir, 'pages': 0}

    unsafe = _unsafe_output_reason(out_dir, archive_root, fha_config)
    if unsafe:
        return {'status': 'bad-output', 'messages': [unsafe], 'out_dir': out_dir, 'pages': 0}

    # Archive protection first (the message above is more specific), then
    # ownership: a non-empty folder fha site never built into is refused
    # BEFORE anything opens or writes, so its contents stay intact.
    unowned = _unowned_output_reason(out_dir)
    if unowned:
        return {'status': 'bad-output', 'messages': [unowned], 'out_dir': out_dir, 'pages': 0}

    # Standalone builds refuse a stale index to avoid publishing redacted persons whose
    # living flag was changed since the last `fha index` run.  Linked (dev preview)
    # mode only warns - a slightly stale preview beats no preview.
    conn = open_index_db(archive_root, _REQUIRED_TABLES, strict=not linked)
    if conn is None:
        return {'status': 'no-index', 'messages': [], 'out_dir': out_dir, 'pages': 0}

    builder = _SiteBuilder(conn, archive_root, fha_config, out_dir, linked=linked,
                           workbench=workbench, workbench_context=workbench_context)
    try:
        builder.prepare()
        if dry_run:
            return {
                'status': 'dry-run', 'messages': builder.messages, 'out_dir': out_dir,
                'pages': (len(builder.source_pages) + len(builder.person_pages)
                          + len(builder.place_pages) + 2),
                # What a real rebuild would first clear from the output dir -
                # the preview half of _reset_output's delete (never a warning,
                # so it lives beside 'messages', not in it).
                'reset_preview': builder.reset_preview(),
            }
        try:
            pages = builder.run()
        except OSError as exc:
            # _reset_output raises in standalone mode if stale pages can't be removed.
            msg = (
                f'ERROR: could not clear the previous site output: {exc}. '
                'Close any programs using those files and run `fha site` again.'
            )
            return {'status': 'reset-failed', 'messages': [msg], 'out_dir': out_dir, 'pages': 0}
        return {'status': 'ok', 'messages': builder.messages, 'out_dir': out_dir, 'pages': pages}
    finally:
        builder.close()
        conn.close()


def run_site(
    archive_root: Path,
    out_dir: Path,
    *,
    linked: bool = False,
    dry_run: bool = False,
    workbench: bool = False,
    workbench_context: dict | None = None,
) -> Result:
    """Library entry point. Build the site and return a Result.

    `data` is the `_site_payload` dict ({'status', 'messages', 'out_dir',
    'pages'}); Result exposes dict-style access (_lib.py), so callers keep
    reading `result['status']` / `result['pages']` unchanged.  A real build lists
    the written output directory in `changed`; a --dry-run (status 'dry-run')
    writes nothing and leaves `changed` empty.

    `workbench` (serve only - never exposed on the `fha site` CLI) turns on the
    editing chrome and the /root/ asset-href rewrite. It REQUIRES `linked`:
    workbench+standalone is refused here, because the workbench is the private,
    unredacted local view by definition. `workbench_context` carries serve's
    runtime values (port, CSRF token, review/inbox counts) baked into the bar.
    """
    if workbench and not linked:
        return Result(
            ok=False, exit_code=EXIT_FAILURE,
            data={'status': 'bad-config', 'out_dir': str(out_dir), 'pages': 0,
                  'messages': ['workbench mode requires linked mode (it is the '
                               'unredacted local view). This is an internal serve '
                               'call - report it as a bug.']},
        ).add('error', 'workbench mode requires linked mode.')
    if is_working_copy(archive_root):
        # Warning-level refusal, not a failure: ok stays True, exit stays clean,
        # data.status='working-copy' is the machine discriminator (TOOLING §13d).
        # data['messages'] carries the human-facing text (as _cmd_site prints
        # it, same as every other status); .add() below is for headless
        # callers reading Result.messages.
        warning_text = (
            'fha site is not available in working-copy mode - '
            'the photo and document files are on the main machine. '
            'Build the site there.'
        )
        return Result(
            ok=True,
            exit_code=EXIT_CLEAN,
            data={'status': 'working-copy', 'out_dir': str(out_dir), 'pages': [],
                  'messages': [warning_text]},
        ).add(
            'warning',
            warning_text,
        )
    payload = _site_payload(archive_root, out_dir, linked=linked, dry_run=dry_run,
                            workbench=workbench, workbench_context=workbench_context)
    status = payload['status']
    changed = [str(payload['out_dir'])] if status == 'ok' else []
    # Mirror _cmd_site's per-status exit codes so headless callers returning
    # Result.exit_code see a failed build as a failure, not a clean 0.
    if status in ('ok', 'dry-run'):
        exit_code = EXIT_WARNINGS if payload.get('messages') else EXIT_CLEAN
    else:  # no-jinja, no-index, bad-config, bad-output, reset-failed
        exit_code = EXIT_FAILURE
    return Result(ok=(status in ('ok', 'dry-run')), exit_code=exit_code,
                  data=payload, changed=changed)


def _unsafe_output_reason(out_dir: Path, archive_root: Path, fha_config: dict) -> str | None:
    """Return a plain refusal message if writing the site to `out_dir` would
    overwrite or pollute archive content, else None.

    `fha site` clears its owned subtrees (`persons/`, `sources/`, `places/`,
    `media/`, `data/`, `vendor/`) of the output directory before regenerating
    (idempotent rebuild). Two of those - `sources/` and `places/` - share names
    with the archive's own record trees, so pointing `--out` at the archive root
    would delete real records. And building *into* a record or asset tree (e.g.
    `--out sources`) would scatter generated pages among the originals. Refuse
    both before any write. The default `generated/site/` is always safe.
    """
    try:
        out_res = out_dir.resolve()
        root_res = archive_root.resolve()
    except OSError:
        return None
    if out_res == root_res:
        return (
            f'Refusing to build the site into the archive root ({archive_root}). '
            'The site clears its own sources/ folder when it rebuilds, which would '
            'delete your records. Pick a separate folder, e.g. `--out generated/site` (the default).'
        )
    # A different archive (its own fha.yaml + record tree) must not be clobbered.
    if (out_dir / 'fha.yaml').exists():
        return (
            f'Refusing to build the site into {out_dir}: it looks like another archive '
            '(it has an fha.yaml). Choose an empty or site-only folder, e.g. the default `generated/site`.'
        )
    # Building at or inside a record/asset tree would pollute the originals.
    protected = ['sources', 'people', 'places', 'notes', 'inbox']
    candidates = [archive_root / name for name in protected]
    for alias in ('documents', 'photos'):
        try:
            candidates.append(resolve_path(alias, fha_config, archive_root))
        except Exception:
            pass
    for cand in candidates:
        try:
            cand_res = cand.resolve()
        except OSError:
            continue
        if out_res == cand_res or cand_res in out_res.parents:
            return (
                f'Refusing to build the site into {out_dir}: that is inside your archive\'s '
                f'"{cand.name}" folder, where it would mix generated pages in with your originals. '
                'Choose a separate folder, e.g. the default `generated/site`.'
            )
    return None


def _unowned_output_reason(out_dir: Path) -> str | None:
    """Refuse a non-empty output dir that fha site did not create, else None.

    Why: `_reset_output` clears generically named subtrees (sources/, media/,
    data/, ...) plus index.html/discoveries.html inside the output dir. Pointed
    at a folder that merely happens to contain such names (say `--out
    ~/Documents`), that clearing would delete files that were never the
    site's. So every successful build stamps the dir with a `.fha-site`
    marker file, and a rebuild proceeds only when the target is brand new,
    empty, or marked.

    Back-compat: a site built before the marker existed carries no
    `.fha-site`, but is recognizable by its own output shape - an index.html
    plus the vendored `vendor/fha-tree.js` renderer. Such a folder is
    accepted, and gains the marker when this rebuild finishes.
    """
    if not out_dir.exists():
        return None
    if not out_dir.is_dir():
        return (
            f'Refusing to build the site at {out_dir}: that is a file, not a folder. '
            'Point --out at a new or empty folder.'
        )
    if (out_dir / _SITE_MARKER_NAME).exists():
        return None
    try:
        has_entries = any(out_dir.iterdir())
    except OSError as e:
        # Fail closed: if the folder cannot even be listed, it cannot be
        # safely cleared either.
        return (
            f'Could not check the site output folder {out_dir} ({e}). '
            'Fix the folder permissions, or point --out at a new folder.'
        )
    if not has_entries:
        return None
    if (out_dir / 'index.html').is_file() and (out_dir / 'vendor' / 'fha-tree.js').is_file():
        return None   # a pre-marker fha site build (see docstring)
    return (
        f"Refusing to build the site into {out_dir}: that folder has files in it and "
        "wasn't created by fha site (no .fha-site marker), so rebuilding could delete "
        "files that are not the site's. Point --out at a new or empty folder, or "
        'delete that folder yourself first if you no longer need its contents.'
    )


def _display_path(p: Path, archive_root: Path) -> str:
    try:
        return str(p.relative_to(archive_root))
    except ValueError:
        return str(p)


def _cmd_site(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    # Deliverables live under a visible `generated/` parent (the databases stay in
    # the hidden `.cache/`). Standalone and the linked preview default to separate
    # subfolders so a preview build never overwrites the shareable snapshot.
    default_sub = 'site-linked' if getattr(args, 'linked', False) else 'site'
    out_dir = Path(getattr(args, 'out', None) or Path('generated') / default_sub)
    if not out_dir.is_absolute():
        out_dir = archive_root / out_dir

    result = run_site(
        archive_root, out_dir,
        linked=getattr(args, 'linked', False),
        dry_run=getattr(args, 'dry_run', False),
    )

    for m in result['messages']:
        print(m, file=sys.stderr)

    status = result['status']
    if status == 'no-jinja':
        print(
            'ERROR: building the site needs Jinja2. Install it with '
            f'`{pip_command("jinja2")}`, then run `fha site` again.',
            file=sys.stderr,
        )
        return EXIT_FAILURE
    if status == 'no-index':
        return EXIT_FAILURE   # open_index_db already printed the cause + fix
    if status == 'bad-config':
        return EXIT_FAILURE   # the config error message is already in result['messages']
    if status == 'bad-output':
        return EXIT_FAILURE   # the refusal message is already in result['messages']
    if status == 'reset-failed':
        return EXIT_FAILURE   # the OSError detail is already in result['messages']
    if status == 'working-copy':
        return EXIT_CLEAN   # the refusal warning is already in result['messages']

    mode = 'linked preview' if getattr(args, 'linked', False) else 'standalone snapshot'
    where = _display_path(result['out_dir'], archive_root)
    if status == 'dry-run':
        print(f'(dry run - no files written) Would build {result["pages"]} pages ({mode}) in {where}')
        preview = result.get('reset_preview') or []
        if preview:
            print(f'A real build would first remove these from {where}: ' + ', '.join(preview))
        else:
            print('Nothing from a previous build to remove there.')
        return EXIT_WARNINGS if result['messages'] else EXIT_CLEAN

    print(f'Site built: {result["pages"]} pages ({mode}) in {where}')
    if not getattr(args, 'linked', False) and not _PIL_AVAILABLE:
        print('Note: Pillow is not installed, so images were omitted. Install it with '
              f'`{pip_command("pillow")}` for photos in the standalone site.', file=sys.stderr)
    return EXIT_WARNINGS if result['messages'] else EXIT_CLEAN


def _add_site_args(p: argparse.ArgumentParser) -> None:
    p.add_argument('--out', metavar='PATH', dest='out',
                   help='Output directory (default: generated/site/, or generated/site-linked/ with --linked).')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--standalone', dest='linked', action='store_false',
                      help='Self-contained, redacted snapshot safe to share (default).')
    mode.add_argument('--linked', dest='linked', action='store_true',
                      help='Local developer preview: real paths, no copies, no redaction.')
    p.set_defaults(linked=False)
    p.add_argument('--dry-run', action='store_true', dest='dry_run',
                   help='Report how many pages would be built and what a rebuild would '
                        'first remove from the output folder, without writing anything.')
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Build a browsable family website you can open in any browser.

  fha site                Build the shareable snapshot (redacted, self-contained)
  fha site --standalone   The same shareable snapshot, named explicitly
  fha site --linked       An unredacted local preview (for yourself, not to share)

Opens from a plain file, no server needed - want to see your tree? build the
site and open it. Living people and restricted material are redacted by default."""


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = subs.add_parser(
        'site',
        help='Generate the static HTML family explorer (standalone snapshot or linked preview).',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_site_args(p)
    p.set_defaults(func=_cmd_site)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha site', description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_site_args(parser)
    parser.set_defaults(func=_cmd_site)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
