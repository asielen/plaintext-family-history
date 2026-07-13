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
standalone redaction audit enforced by the page-set design below), and M8.5
(interactive trees - a vendored, dependency-free renderer fed the neutral tree
JSON through a single adapter seam).

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
    _inline_html               - inline pass: links, [ID] tokens, **bold**
    _extract_section           - pull one `## Heading` section body from a record

  Dates
    _decade_header             - EDTF date → "1880s" decade label (timeline grouping)

  Image derivatives
    _PIL_AVAILABLE             - is Pillow importable?
    _make_derivative           - resized, EXIF-stripped JPEG/PNG copy (standalone)

  Static charts (person page)
    _render_fan_svg            - radial ancestor fan, self-contained SVG
    _render_pedigree_svg       - horizontal family chart: children - subject/spouse(s)
                                 - parents - grandparents, self-contained SVG

  Paths / hrefs
    _rel_href                  - relative href from a page dir to a target file
    _page_filename             - id → 'p-xxx.html' / 's-xxx.html'
    _json_for_script           - JSON serialized safe for inline <script> embedding

  Interactive tree (M8.5) + shared chart redaction
    _apex_ancestor             - deepest ancestor of root_person (home-tree seed)
    _build_tree_data           - BFS relationships → neutral tree JSON + url + redaction
    _tree_node, _person_vitals - one redacted node; its birth/death labels
    _chart_entry               - one redacted {name,url,dates} node; shared by the
                                 Ahnentafel walk and the family-wings walk below
    _build_ahnentafel          - parent-edge walk → Ahnentafel map (fan + pedigree)
    _build_family_wings        - spouse/child edges → pedigree's family-chart columns
    _make_tree_ctx             - build a tree, write data/tree_*.json, return template ctx
    _copy_vendor               - copy the vendored renderer/adapter into the site

  Builder
    _SiteBuilder               - holds conn, mode, maps, page sets, jinja env
      .prepare                 - load persons/sources, decide which pages exist
      .render_token            - one [ID] token → HTML (link / redaction / mark)
      .build_source_page       - M8.1 source page
      .build_person_page       - M8.2 person page
      .build_index_page        - minimal people+sources landing page
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
    ASSET_ROOT_ALIASES,
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    PROVISIONAL_VITAL_FIELDS,
    Result,
    FhaConfigError,
    apply_private_fence,
    configure_utf8_stdout,
    fmt_id_display,
    id_type_of,
    is_genetic_parent_subtype,
    is_working_copy,
    load_fha_yaml,
    normalize_id,
    strip_link_wrapper,
    strip_unaccepted_drafts,
    open_index_db,
    photoindex_status,
    read_record,
    resolve_path,
    resolve_root_arg,
)

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

# Ancestor pedigree depth on person pages (M8.5: "3 generations default" =
# subject + 2 parent hops). The home descendant explorer is uncapped (the
# vendored renderer collapses large trees on demand).
_PEDIGREE_GENERATIONS = 3

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


# Inline constructs, tried left to right. A markdown link `[text](url)` is
# matched before a token so a token never half-matches a link; the `[[ ]]`
# wikilink is matched before the legacy single-bracket `[ID]`; bold is last.
# Anything not matched is literal text and gets escaped.
_INLINE_RE = re.compile(
    r'\[(?P<ltext>[^\]]+)\]\((?P<lurl>[^)\s]+)\)'                 # [text](url)
    r'|\[\[(?P<wtarget>[^\[\]|#]+)(?:#[^\[\]|]*)?(?:\|(?P<wdisp>[^\[\]]*))?\]\]'  # [[target|disp]]
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
    """
    out: list[str] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        out.append(_escape(text[pos:m.start()]))
        pos = m.end()
        if m.group('wtarget') is not None:
            out.append(render_token(m.group('wtarget').strip(), m.group('wdisp')))
        elif m.group('token'):
            out.append(render_token(m.group('token')))
        elif m.group('ltext') is not None:
            href = _safe_link_href(m.group('lurl'))
            if href is not None:
                out.append(f'<a href="{href}">{_escape(m.group("ltext"))}</a>')
            else:
                out.append(_escape(m.group('ltext')))
        else:  # bold
            out.append(f'<strong>{_escape(m.group("bold"))}</strong>')
    out.append(_escape(text[pos:]))
    return ''.join(out)


_HEADING_RE = re.compile(r'^(#{1,6})\s+(.*)$')
_LIST_RE = re.compile(r'^\s*[-*]\s+(.*)$')
# A photo embed on its own line: `![[S-id|Caption]]` (Obsidian embed syntax).
# Renders as a figure; the id resolves to a photo through the index. Caption optional.
_EMBED_RE = re.compile(r'^!\[\[\s*([^\]|]+?)\s*(?:\|\s*([^\]]*?)\s*)?\]\]\s*$')


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
    (reading outward) on the narrow outer ones - and are truncated to fit.
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
        fs = max(8.0, min(fs_max, avail / (max(1, len(full)) * _CW)))
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


def _render_pedigree_svg(labels: dict, spouses: list[dict] | None = None,
                          children: list[dict] | None = None) -> str:
    """Render a horizontal (left→right) family pedigree as a self-contained SVG.

    `labels` is an Ahnentafel map {number: {'name','url','redacted','dates'}} covering
    two generations up - slot 1 the subject, 2/3 the parents, 4-7 the grandparents
    (see `_build_ahnentafel`, called with max_gen=2). `spouses`/`children` are the
    win-1 family-chart extension: plain lists of the same {'name','url','dates'}
    shape (from `_build_family_wings`), never containing a redacted person - that
    filtering happens upstream, so unlike an ancestor slot a redacted spouse/child
    has no faint 'Unknown' placeholder to fall back on and is simply absent.

    Layout, left to right: children (if any) - subject + spouse(s), stacked in one
    column - parents - grandparents. This is the ancestors-only chart's original
    shape with two columns bolted on either side of the subject; when spouses and
    children are both empty the geometry (column x, row y, viewBox) is bit-for-bit
    what the ancestors-only renderer produced before this win, so an existing
    person's pedigree does not visually change. The subject sits at the left of
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
    sizing without pulling in any of its workbench affordances."""
    spouses = spouses or []
    children = children or []
    has_children_col = bool(children)

    CW = 176
    if has_children_col:
        CH, COL_GAP, ROW, PAD = 48, 16, 60, 8
    else:
        CH, COL_GAP, ROW, PAD = 62, 40, 72, 8

    # Generation index 0 is the subject/spouse column; ancestors step positive
    # (1 = parents, 2 = grandparents); children, when present, take -1 so they
    # sit to the left of the subject as the wireframe lays out.
    min_gen = -1 if has_children_col else 0

    def col_x(gen: int) -> float:
        return PAD + (gen - min_gen) * (CW + COL_GAP)

    def row_index(num: int) -> float:
        """Row position (in ROW units, subject = 1.5) for an Ahnentafel slot -
        the same numbers the pre-win-1 renderer used, kept as a pure function
        so spouse/children rows can be placed relative to the same scale."""
        g = num.bit_length() - 1
        if g == 0:                                   # subject
            return 1.5
        if g == 1:                                   # parent: centred over its 2 grandparents
            return 0.5 if num == 2 else 2.5
        return float(num - 4)                         # grandparents: four stacked rows

    # Draw the subject always; an ancestor slot only when its child is a drawn person -
    # real ancestors as name cards, a known person's missing parent as a faint 'Unknown'.
    render: dict[int, tuple] = {1: ('person', labels.get(1) or {'name': ''})}
    for slot in (2, 3, 4, 5, 6, 7):
        if render.get(slot // 2, ('', None))[0] != 'person':
            continue
        lab = labels.get(slot)
        render[slot] = ('person', lab) if (lab and lab.get('name')) else ('empty', None)

    subject_row = 1.5
    spouse_rows = [subject_row + 1 + i for i in range(len(spouses))]     # stack below the subject
    n_children = len(children)
    # Centred on the subject's row so a small family reads as balanced, not
    # lopsided - matches the wireframe centring children on the couple.
    children_rows = [subject_row + (i - (n_children - 1) / 2) for i in range(n_children)]

    # The ancestor band has always been rendered at a fixed size (rows 0-3,
    # the full grandparent grid) whenever any ancestor slot beyond the
    # subject has data - regardless of how many of those slots are actually
    # filled - preserved here so an ancestors-only chart's canvas is
    # unchanged. A family-only chart (spouse/children but zero known
    # ancestors) has no reason to reserve that band, so it starts tight
    # around the subject's own row instead. Spouse/children rows then extend
    # whichever starting band only when they actually reach beyond it (extra
    # spouses stacking past row 3, a wide brood of children reaching above
    # row 0).
    ancestor_band = [0.0, 3.0] if len(labels) > 1 else [subject_row]
    all_rows = ancestor_band + spouse_rows + children_rows
    min_row, max_row = min(all_rows), max(all_rows)
    base = PAD + CH / 2 - min_row * ROW

    def y_center(row: float) -> float:
        return base + row * ROW

    max_gen = max((k.bit_length() - 1 for k in render), default=0)
    W = 2 * PAD + (max_gen - min_gen + 1) * CW + (max_gen - min_gen) * COL_GAP
    H = 2 * PAD + CH + (max_row - min_row) * ROW

    def yr(edtf) -> str:
        m = re.search(r'\d{4}', str(edtf)) if edtf else None
        return m.group(0) if m else ''

    def card(x: float, yc: float, cls_extra: str, lab: dict | None) -> str:
        if lab is None:
            cls, inner = 'ped-node ped-empty', '<span class="ped-name">Unknown</span>'
        else:
            cls = 'ped-node' + cls_extra
            name = html.escape(lab.get('name') or '')
            url = lab.get('url')
            name_el = (f'<a class="ped-name" href="{html.escape(url, quote=True)}">{name}</a>'
                       if url else f'<span class="ped-name">{name}</span>')
            d = lab.get('dates') or {}
            b, dd = yr(d.get('birth')), yr(d.get('death'))
            span = f'{b}–{dd}' if (b and dd) else (f'b. {b}' if b else (f'd. {dd}' if dd else ''))
            inner = name_el + (f'<span class="ped-dates">{span}</span>' if span else '')
        return (f'<foreignObject x="{x:.0f}" y="{yc - CH / 2:.0f}" width="{CW}" height="{CH}">'
                f'<div xmlns="http://www.w3.org/1999/xhtml" class="{cls}">{inner}</div>'
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
        cards.append(card(x, yc, ' ped-self' if slot == 1 else '', None if kind == 'empty' else lab))

    subj_x = col_x(0)
    subj_y = y_center(subject_row)
    for i, lab in enumerate(spouses):
        cards.append(card(subj_x, y_center(spouse_rows[i]), '', lab))
    for i, lab in enumerate(children):
        cards.append(card(col_x(-1), y_center(children_rows[i]), '', lab))

    if children:
        # A trunk between the children column and the subject/spouse column:
        # one vertical spine plus a horizontal tick to every child and to the
        # subject and each spouse - reads as "these children belong to this
        # family", without asserting which specific spouse is the other
        # parent (the data model does not record that).
        trunk_x = (col_x(-1) + CW + subj_x) / 2
        child_ys = [y_center(r) for r in children_rows]
        family_ys = [subj_y] + [y_center(r) for r in spouse_rows]
        trunk_ys = child_ys + family_ys
        if len(set(trunk_ys)) > 1:
            links.append(f'<path class="ped-link" d="M{trunk_x:.0f},{min(trunk_ys):.0f} '
                         f'V{max(trunk_ys):.0f}"/>')
        for cy in child_ys:
            links.append(f'<path class="ped-link" d="M{col_x(-1) + CW:.0f},{cy:.0f} H{trunk_x:.0f}"/>')
        for fy in family_ys:
            links.append(f'<path class="ped-link" d="M{trunk_x:.0f},{fy:.0f} H{subj_x:.0f}"/>')
    elif spouses:
        # No children to route through a trunk - a direct bracket at the
        # column's left edge is enough to show the subject and spouse(s) as
        # one family unit.
        family_ys = [subj_y] + [y_center(r) for r in spouse_rows]
        if len(set(family_ys)) > 1:
            links.append(f'<path class="ped-link" d="M{subj_x:.0f},{min(family_ys):.0f} '
                         f'V{max(family_ys):.0f}"/>')

    svg_cls = 'pedigree pedigree-family' if has_children_col else 'pedigree'
    label = 'Family chart' if (spouses or children) else 'Ancestor pedigree'
    return (f'<svg class="{svg_cls}" viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid meet" '
            f'role="img" aria-label="{label}">' + ''.join(links) + ''.join(cards) + '</svg>')


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
        self.restricted_names: dict[str, set[str]] = {}   # pid → lowercased restricted variant values
        # Opened once in prepare() when the photo index is fresh, reused across
        # every person page, closed by run_site - so the photos-root freshness
        # walk happens once per build, not once per curated person.
        self.photos_conn: sqlite3.Connection | None = None
        # discoveries.md is read for both the discoveries page and the home
        # teaser; memoize so the file is parsed once per build.
        self._discoveries: tuple[str, list[str]] | None = None
        # A person's profile photo is resolved once (front-matter read + photo
        # lookup + derivative) and reused across their page and every tree node
        # they appear in. Value: the publishable image file, or None.
        self._profile_photo_cache: dict[str, Path | None] = {}
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
            'SELECT id, title, source_type, date_edtf, repository, source_class, '
            'restricted, publication_ok, status, path FROM sources'
        ):
            self.source_meta[row['id']] = row
        for row in self.conn.execute('SELECT id, name, hierarchy, within, lat, lon FROM places'):
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
            if (row['tier'] or '') != 'curated':
                continue          # stubs/connections get no standalone page (TOOLING §12)
            if self.linked or not self._person_is_redacted(row):
                self.person_pages.add(pid)

        self._open_photos()

    def _load_restriction_markers(self) -> None:
        """Read the claim/person/name-level `restricted` markers from disk.

        The index records `restricted` only on sources, and only as 0/1, so the
        person-level marker, the per-claim marker, the per-name-variant marker,
        and a free-text source type (`restricted: by-request`) are all invisible
        to it. This one pass over the person and source records fills the four
        sets the redaction predicates consult. A record that cannot be read is
        skipped (its page still builds; the standalone audit catches any leak)."""
        for pid, row in self.person_meta.items():
            if not row['path']:
                continue
            try:
                rec = read_record(self.archive_root / row['path'])
            except Exception:
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
                rec = read_record(self.archive_root / row['path'])
            except Exception:
                continue
            if _is_restricted_value(rec['meta'].get('restricted')):
                self.restricted_sources.add(sid)
            for claim in rec['claims']:
                if not isinstance(claim, dict):
                    continue
                cid = normalize_id(str(claim.get('id', '')))
                if cid and _is_restricted_value(claim.get('restricted')):
                    self.restricted_claims.add(cid)

    def _open_photos(self) -> None:
        """Open the photo index once if it is fresh, for the person photo strips.

        The freshness check (`photoindex_status`) walks the whole photos root,
        so it must run once per build - never once per person. An absent, stale,
        or unreadable photo index simply leaves `self.photos_conn` None and the
        photo strip is omitted (it is enrichment, never a build blocker).
        """
        status, _lag = photoindex_status(self.archive_root, self.fha_config)
        if status != 'fresh':
            return
        try:
            conn = sqlite3.connect(str(self.archive_root / '.cache' / 'photos.sqlite'))
            conn.row_factory = sqlite3.Row
            self.photos_conn = conn
        except sqlite3.DatabaseError:
            self.photos_conn = None

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
        # (TOOLING §12 / BUILD M8.1; these are already lint errors).
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
        the `[L-id]`-token link in prose - the symmetry the review flagged."""
        label = self._place_label(place_text, place_id)
        if not label:
            return self._markup('')
        if place_id and place_id in self.place_pages:
            href = html.escape(_rel_href(self.places_dir / _page_filename(place_id), page_dir), quote=True)
            return self._markup(f'<a href="{href}">{_escape(label)}</a>')
        return self._markup(_escape(label))

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

    def _file_entry(self, asset_rel: str, role: str | None, page_dir: Path) -> dict | None:
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
        """
        label = Path(asset_rel).name
        try:
            resolved = resolve_path(asset_rel, self.fha_config, self.archive_root)
        except Exception:
            return {'label': label, 'note': 'asset path could not be resolved', 'link_href': None, 'thumb_href': None}
        is_image = resolved.suffix.lower() in _IMAGE_SUFFIXES
        role_note = f'role: {role}' if role else None

        if not resolved.exists():
            return {'label': label, 'note': 'file not available in this build', 'link_href': None, 'thumb_href': None}

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
                return {'label': label, 'note': 'image omitted (Pillow not installed)', 'link_href': None, 'thumb_href': None}
            return None  # signal: caller handles derivative creation (needs sid)
        return {'label': label, 'note': (role_note + ' · ' if role_note else '') + 'original kept in the archive',
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

    def _standalone_image_entry(self, sid: str, asset_rel: str, role: str | None, page_dir: Path) -> dict:
        """Create the media derivative for a standalone image asset and return
        its file entry. Split from `_file_entry` because it needs the source id
        for the media subfolder and may emit a warning into `self.messages`."""
        resolved = resolve_path(asset_rel, self.fha_config, self.archive_root)
        dest = self._media_dest(asset_rel, normalize_id(sid))
        if _make_derivative(resolved, dest):
            href = _rel_href(dest, page_dir)
            return {'label': Path(asset_rel).name, 'note': f'role: {role}' if role else None,
                    'link_href': href, 'thumb_href': href}
        self.messages.append(f'WARNING: could not build a web image for {asset_rel} (skipped, build continues)')
        return {'label': Path(asset_rel).name, 'note': 'image could not be processed', 'link_href': None, 'thumb_href': None}

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
        try:
            rec = read_record(self.archive_root / row['path'])
            citation = ' '.join(str(rec['meta'].get('citation', '') or '').split())
            if rec['parse_errors']:
                self.messages.append(
                    f'WARNING: {row["path"]} has a formatting problem '
                    f'({rec["parse_errors"][0][1]}); showing the title in place of its citation.'
                )
        except Exception as e:
            self.messages.append(f'WARNING: could not read {row["path"]} ({e}); showing the title only.')

        # A standalone snapshot publishes only the archive's current position -
        # accepted + needs-review. `suggested` (unreviewed AI drafts; "your
        # suggestions are not facts") and `rejected`/`superseded` (known not
        # current) are withheld from public output, matching the timeline's
        # rule. `--linked` (developer preview) shows every status with its badge.
        status_filter = '' if self.linked else "AND status IN ('accepted', 'needs-review')"
        living_filter = (
            '' if self.linked else
            "AND NOT EXISTS ("
            "  SELECT 1 FROM claim_persons cp2 JOIN persons p ON cp2.person_id = p.id "
            "  WHERE cp2.claim_id = c.id AND p.living IN ('true','unknown')"
            ")"
        )
        claims = []
        for c in self.conn.execute(
            'SELECT id, type, value, date_edtf, place_id, place_text, status FROM claims c '
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
                'type': c['type'], 'value': c['value'], 'date': c['date_edtf'] or '',
                'place': self._place_html(c['place_text'], c['place_id'], page_dir),
                'persons_html': self._markup(persons_html), 'status': c['status'],
                # Workbench-only: the C-id drives the inline claim actions. Never
                # used in standalone output (the template gates on `workbench`).
                'claim_id': fmt_id_display(c['id']),
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
            # Workbench-only (template gates on `workbench`): S-id + record path.
            'source_id': fmt_id_display(sid), 'record_relpath': row['path'],
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
            'SELECT path, role FROM source_files WHERE source_id = ?', (sid,)
        ):
            if not f['path']:
                continue
            # Standalone: skip images co-tagged to a living person in the photo catalog.
            if not self.linked and self._is_living_tagged_photo(f['path']):
                entries.append({
                    'label': Path(f['path']).name,
                    'note': 'image omitted - tagged to a living person',
                    'link_href': None,
                    'thumb_href': None,
                })
                continue
            entry = self._file_entry(f['path'], f['role'], page_dir)
            if entry is None:   # standalone image needing a derivative
                entry = self._standalone_image_entry(sid, f['path'], f['role'], page_dir)
            entries.append(entry)
            is_image = Path(f['path']).suffix.lower() in _IMAGE_SUFFIXES
            if is_image and entry.get('thumb_href'):
                candidates.append(((f['role'] or '').strip().lower() == 'front', entry))
        portrait = next((e for is_front, e in candidates if is_front), None)
        if portrait is None and candidates:
            portrait = candidates[0][1]
        return entries, portrait

    # - person page (M8.2) -

    def build_person_page(self, pid: str) -> None:
        """Render one curated person page (TOOLING §12 / M8.2)."""
        row = self.person_meta[pid]
        page_dir = self.persons_dir
        self._footnotes = {}          # start this page's source-footnote numbering
        self._footnote_seq = []

        summary = self._person_summary(pid, page_dir)
        biography_html, stories_html, research_html = self._person_prose(row, page_dir)
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
        ahnen = self._build_ahnentafel(pid, _FAN_GENERATIONS, page_dir)
        ped_labels = {n: e for n, e in ahnen.items() if n < 8}
        wings = self._build_family_wings(pid, page_dir)
        has_pedigree = len(ped_labels) > 1 or wings['spouses'] or wings['children']
        pedigree = (self._markup(_render_pedigree_svg(ped_labels, wings['spouses'], wings['children']))
                   if has_pedigree else None)
        # Same condition _render_pedigree_svg uses for its SVG aria-label (a
        # sighted reader on the page and a screen-reader user on the SVG must
        # be told the same truth about what the chart contains): a subject
        # with a recorded spouse or child gets the family-chart heading, an
        # ancestors-only chart gets the honest 'Ancestors' heading instead of
        # the old unconditional 'Family'.
        chart_title = 'Family' if (wings['spouses'] or wings['children']) else 'Ancestors'
        fan = self._markup(_render_fan_svg(ahnen, _FAN_GENERATIONS)) if len(ahnen) > 1 else None

        ctx = {
            'display_id': fmt_id_display(pid), 'name': name,
            'alt_names': alt_names, 'tags': tags,
            'portrait': self._profile_photo_href(pid, page_dir),
            'family_strip': self._person_family_strip(pid, page_dir),
            'pedigree': pedigree,
            'chart_title': chart_title,
            'fan': fan,
            'summary': summary,
            'biography_html': self._markup(biography_html) if biography_html else None,
            'stories_html': self._markup(stories_html) if stories_html else None,
            'research_html': self._markup(research_html) if research_html else None,
            'timeline': timeline, 'sources': sources, 'family': family, 'photos': photos,
            # Workbench-only fields (harmless in standalone - the template gates
            # every use on `workbench`): the record's on-disk relpath for the
            # "open file" button and living value for the "change..." affordance.
            'record_relpath': row['path'],
            'living': (row['living'] or 'unknown'),
            'milestone_sources': self._person_milestone_sources(pid) if self.workbench else [],
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
            meta = read_record(self.archive_root / row['path'])['meta']
        except Exception:
            return [], []
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

    def _person_summary(self, pid: str, page_dir: Path) -> list[dict]:
        """Accepted vital claims as the summary block (birth/death/marriage/…)."""
        living_filter = (
            '' if self.linked else
            "AND NOT EXISTS ("
            "  SELECT 1 FROM claim_persons cp2 JOIN persons p ON cp2.person_id = p.id "
            "  WHERE cp2.claim_id = c.id AND p.living IN ('true','unknown')"
            ")"
        )
        rows = self.conn.execute(
            "SELECT c.id, type, value, date_edtf, place_id, place_text, source_id FROM claims c "
            "JOIN claim_persons cp ON c.id = cp.claim_id "
            f"WHERE cp.person_id = ? AND c.status = 'accepted' "
            f"AND c.type IN ('birth','death','marriage','baptism','burial') {living_filter}",
            (pid,),
        ).fetchall()
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
                summary.append({
                    'label': _VITAL_LABELS[t],
                    'value': r['date_edtf'] or r['value'] or '',
                    'place': self._place_html(r['place_text'], r['place_id'], page_dir),
                    'source_html': self._markup(self._source_link(r['source_id'], page_dir)) if r['source_id'] else '',
                    'provisional': False,
                })
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
            for t in sorted(PROVISIONAL_VITAL_FIELDS):
                if t in by_type:
                    continue   # a sourced claim wins - the estimate is superseded
                est = self._provisional_vital(pid, t)
                if est:
                    summary.append({
                        'label': _VITAL_LABELS[t],
                        'value': est,
                        'place': '',
                        'source_html': '',
                        'provisional': True,
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
        is read from the record file on demand and only in workbench mode."""
        row = self.person_meta.get(pid)
        if not row:
            return None
        try:
            meta = read_record(self.archive_root / row['path'])['meta']
        except Exception:
            return None
        val = meta.get(field)
        return str(val).strip() if val not in (None, '') else None

    def _person_prose(self, row: sqlite3.Row, page_dir: Path) -> tuple[str, str, str]:
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
            rec = read_record(self.archive_root / row['path'])
        except Exception as e:
            self.messages.append(f'WARNING: could not read {row["path"]} ({e}); skipping its prose.')
            return '', '', ''
        render = lambda tok, disp=None: self.render_token(tok, page_dir, disp)  # noqa: E731 - tiny closure
        embed = lambda t, c: self._render_embed(t, c, page_dir)  # noqa: E731
        # Apply the `<!-- private -->` fence to the whole body BEFORE section
        # extraction. Otherwise an opener that sits above a `## Research Notes`
        # heading is dropped with its parent section, and the extracted body
        # sees only the trailing `<!-- /private -->` - leaving the private
        # text unfenced and publishable on a standalone build.
        body = rec['body']
        stories = rec['stories']
        dp = not self.linked
        if body:
            body = apply_private_fence(body, drop=dp)
        if stories:
            stories = apply_private_fence(stories, drop=dp)
        bio = _extract_section(body, 'Biography')
        research = _extract_section(body, 'Research Notes')
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
            return '', '', ''
        # Private fences were already applied to the whole body above, so
        # _prose_to_html need not re-apply them here.
        biography_html = _prose_to_html(bio, render, embed, drop_private=dp) if bio else ''
        stories_html = _prose_to_html(stories, render, embed, drop_private=dp) if stories else ''
        research_html = _prose_to_html(research, render, embed, drop_private=dp) if research else ''
        return biography_html, stories_html, research_html

    def _person_timeline(self, pid: str, page_dir: Path) -> list[dict]:
        """Accepted + needs-review claims, grouped by decade (TOOLING §12 - the
        same query and shape as `fha views timeline`'s main chronology; suggested
        claims are excluded from the published timeline)."""
        living_filter = (
            '' if self.linked else
            "AND NOT EXISTS ("
            "  SELECT 1 FROM claim_persons cp2 JOIN persons p ON cp2.person_id = p.id "
            "  WHERE cp2.claim_id = c.id AND p.living IN ('true','unknown')"
            ")"
        )
        rows = self.conn.execute(
            "SELECT DISTINCT c.id, c.date_edtf, c.date_min, c.type, c.value, c.place_id, c.place_text, c.source_id "
            "FROM claim_persons cp JOIN claims c ON cp.claim_id = c.id "
            f"WHERE cp.person_id = ? AND c.status IN ('accepted','needs-review') {living_filter} "
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
        groups: list[dict] = []
        current: str | None = '\x00'   # sentinel distinct from None (undated)
        entries: list[dict] = []
        for r in rows:
            decade = _decade_header(r['date_edtf'])
            if decade != current:
                if entries:
                    groups.append({'decade': None if current == '\x00' else current, 'entries': entries})
                current = decade
                entries = []
            entries.append({
                'date': r['date_edtf'] or '(undated)', 'type': r['type'], 'value': r['value'],
                'place': self._place_html(r['place_text'], r['place_id'], page_dir),
                'source_html': self._markup(self._source_link(r['source_id'], page_dir)) if r['source_id'] else '',
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
        status_filter = '' if self.linked else "AND c.status IN ('accepted','needs-review')"
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
        pair's relationship is not (only living people are redacted outright)."""
        rows = self.conn.execute(
            "SELECT c.id, c.source_id FROM claims c "
            "JOIN claim_persons cp1 ON c.id = cp1.claim_id AND cp1.person_id = ? "
            "JOIN claim_persons cp2 ON c.id = cp2.claim_id AND cp2.person_id = ? "
            "WHERE c.status IN ('accepted','needs-review')",
            (pid1, pid2),
        ).fetchall()
        for r in rows:
            if normalize_id(str(r['id'])) in self.restricted_claims:
                continue
            if not self._source_hard_restricted(r['source_id']):
                return True
        return not rows  # no claims at all → relationship came from YAML directly, show it

    def _is_living_tagged_photo(self, alias_path: str) -> bool:
        """Return True when any person tagged to this photo in the catalog is living/unknown.

        Source-page image derivatives must skip photos co-tagged to living persons
        even when the source itself is otherwise public - the same rule applied
        to person photo strips applies here."""
        if self.photos_conn is None:
            return False
        try:
            rows = self.photos_conn.execute(
                'SELECT person_ref FROM photo_people WHERE path = ?',
                (alias_path,),
            ).fetchall()
        except sqlite3.DatabaseError:
            return False
        for row in rows:
            person = self.person_meta.get(row['person_ref'] or '')
            # A deceased `restricted: by-request` person is redacted too, not just
            # living/unknown - mirror the person photo strips, which gate on the
            # full _person_is_redacted predicate.
            if person and self._person_is_redacted(person):
                return True
        return False

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
        parents = links([(p, pid) for p in parent_ids], None)
        children = links([(c, pid) for c in edge(pid, 'child')], None)
        spouses = links([(s, pid) for s in edge(pid, 'spouse')], None)

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

        if not (parents or spouses or siblings or children):
            return None
        return {'parents': parents, 'spouses': spouses, 'siblings': siblings, 'children': children}

    def _person_photos(self, pid: str, page_dir: Path) -> list[dict]:
        """Photo strip from `.cache/photos.sqlite` (`photo_people`), one entry
        per variation group. Omitted silently when the photo index is absent or
        stale (`self.photos_conn` None) - it is an optional enrichment, never a
        build blocker. Uses the connection opened once in `prepare()`."""
        if self.photos_conn is None:
            return []
        try:
            rows = self.photos_conn.execute(
                'SELECT DISTINCT ph.group_id, ph.path, ph.caption, ph.is_primary, ph.source_id '
                'FROM photo_people pp JOIN photos ph ON pp.path = ph.path '
                'WHERE pp.person_ref = ?',
                (pid,),
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
        if photos_root and '/' not in ref and Path(ref).suffix.lower() in _IMAGE_SUFFIXES:
            pr = Path(photos_root)
            if not pr.is_absolute():
                pr = self.archive_root / pr
            try:
                matches: list[Path] = []
                if pr.is_dir():
                    for m in pr.rglob(ref):
                        if m.is_file():
                            matches.append(m)
                            if len(matches) > 1:
                                break
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
        the file has no catalog entry."""
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
                'SELECT path FROM photos WHERE path = ? OR path LIKE ?',
                (disk.name, '%/' + disk.name)).fetchone()
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
            rec = read_record(self.archive_root / meta['path'])
        except Exception:  # noqa: BLE001 - an unreadable record just yields no portrait
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
        path, then a basename match (so a moved file still resolves). Prefers the
        group's primary variant on ties."""
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
            rows = sorted(rows, key=lambda x: 0 if x['is_primary'] else 1)
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
        unresolvable or withheld reference renders nothing (never a raw id)."""
        href = self._image_href(target, page_dir, 'embeds')
        if not href:
            self.messages.append(f'WARNING: embed {target!r} matched no publishable photo; skipped.')
            return ''
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
        """Render one place page (TOOLING §12 / M8.3): name, coords (a map *URL*,
        no embedded map dependency), dated `history:`, claims naming the place,
        contained micro-places (`within:` children), and the people most often
        associated with it. People links follow the standard redaction rule, and
        the people-frequency list omits redacted persons entirely so a standalone
        place page never links to - or even names - a living person."""
        row = self.place_meta[lid]
        page_dir = self.places_dir
        self._footnotes = None        # place-page sources render as named links, not footnotes

        lat, lon = row['lat'], row['lon']
        map_url = None
        if lat is not None and lon is not None:
            map_url = f'https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=12/{lat}/{lon}'

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
        claim_rows = self.conn.execute(
            "SELECT c.id, c.type, c.value, c.date_edtf, c.date_min, c.source_id FROM claims c "
            f"WHERE c.place_id = ? AND c.status IN ('accepted','needs-review') {living_filter} "
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
                'type': c['type'], 'value': c['value'], 'date': c['date_edtf'] or '',
                'persons_html': self._markup(
                    ', '.join(self._person_link(p['person_id'], page_dir) for p in person_rows)),
                'source_html': self._markup(self._source_link(c['source_id'], page_dir)) if c['source_id'] else '',
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

        ctx = {
            'display_id': fmt_id_display(lid), 'name': row['name'] or fmt_id_display(lid),
            'hierarchy': row['hierarchy'] or '', 'map_url': map_url,
            'alt_names': alt_names, 'history': history, 'claims': claims,
            'people': people, 'micro': micro,
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
        try:
            text = path.read_text(encoding='utf-8')
        except OSError:
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
        Mirrors `fha views tree`'s node vitals (TOOLING §7 D3)."""
        vitals = {'birth': None, 'death': None}
        for r in self.conn.execute(
            "SELECT c.id, c.type, c.date_edtf, c.source_id FROM claims c JOIN claim_persons cp ON c.id = cp.claim_id "
            "WHERE cp.person_id = ? AND c.type IN ('birth','death') AND c.status = 'accepted'",
            (pid,),
        ):
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

    def _apex_ancestor(self, root_pid: str) -> str:
        """Walk `parent` edges up from root_pid and return the deepest ancestor.

        BUILD M8.5 seeds the home tree from "the root person (descendants mode)";
        TOOLING §12 frames the home hero as a "descendant explorer from a root
        *ancestor*". The configured `root_person` is the Ahnentafel proband (the
        youngest), which has no descendants - so a literal descendants-from-proband
        tree would be a single node. Seeding from the apex of the proband's direct
        line (its most distant ancestor) reconciles the two: the explorer fans
        forward across the whole lineage, and it is still derived from the
        configured root person. Ties (two equally-deep ancestors) break on the
        lowest id for determinism; a proband with no recorded parents is its own
        apex."""
        depth = {root_pid: 0}
        queue = deque([root_pid])
        while queue:
            cur = queue.popleft()
            for r in self.conn.execute(
                "SELECT DISTINCT other_id FROM relationships WHERE person_id = ? AND rel = 'parent'",
                (cur,),
            ):
                other = r['other_id']
                if other not in depth:
                    depth[other] = depth[cur] + 1
                    queue.append(other)
        # Deepest ancestor; ties broken by the lowest id so the seed is stable.
        best = root_pid
        for pid, d in depth.items():
            if d > depth[best] or (d == depth[best] and pid < best):
                best = pid
        return best

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

    def _build_tree_data(self, seed: str, mode: str, max_hops: int | None, page_dir: Path) -> dict:
        """BFS the `relationships` graph from `seed` and emit the neutral tree
        JSON (TOOLING §7/§14b) plus a per-node `url`. `descendants` follows
        `child` edges, `ancestors` follows `parent` edges; a visited set guards
        cousin-marriage cycles. Redaction is applied per node in `_tree_node`."""
        rel = 'parent' if mode == 'ancestors' else 'child'
        order = [seed]
        seen = {seed}
        edges: list[dict] = []
        queue: deque[tuple[str, int]] = deque([(seed, 0)])
        while queue:
            cur, hop = queue.popleft()
            if max_hops is not None and hop >= max_hops:
                continue
            for r in self.conn.execute(
                '''SELECT DISTINCT r.other_id, r.claim_id, c.subtype
                   FROM relationships r LEFT JOIN claims c ON r.claim_id = c.id
                   WHERE r.person_id = ? AND r.rel = ?''',
                (cur, rel),
            ):
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

    def _build_ahnentafel(self, seed: str, max_gen: int, page_dir: Path) -> dict:
        """Ahnentafel map {number: {'name','url','redacted'}} for the fan chart,
        walking `parent` edges from the seed. Father (a parent recorded M) takes the
        even slot, mother (F) the odd one; unknown-sex parents fill whatever slot is
        free. Redaction is applied per person - a withheld ancestor becomes a blank
        segment, never a leaked name (mirrors `_tree_node`)."""
        labels: dict[int, dict] = {1: self._chart_entry(seed, page_dir)}
        queue: deque[tuple[int, str]] = deque([(1, seed)])
        seen = {seed}
        while queue:
            num, pid = queue.popleft()
            if num.bit_length() - 1 >= max_gen:
                continue
            parents: list[tuple[str, str]] = []
            for r in self.conn.execute(
                '''SELECT DISTINCT r.other_id, p.sex
                   FROM relationships r JOIN persons p ON r.other_id = p.id
                   WHERE r.person_id = ? AND r.rel = 'parent' ''', (pid,)):
                other = r['other_id']
                # A deceased ancestor without a page (stub) still fills its slot as a
                # name (unlinked); only a living/unknown/restricted ancestor stays a
                # blank redaction, and a no-public-claim edge is skipped.
                ometa = self.person_meta.get(other)
                if not self.linked and (ometa is None or self._person_is_redacted(ometa)
                                        or not self._has_public_claim(pid, other)):
                    continue
                parents.append((other, (r['sex'] or '').upper()))
            father = next((p for p, s in parents if s == 'M'), None)
            mother = next((p for p, s in parents if s == 'F' and p != father), None)
            rest = [p for p, s in parents if p not in (father, mother)]
            if father is None and rest:
                father = rest.pop(0)
            if mother is None and rest:
                mother = rest.pop(0)
            for slot_num, ppid in ((2 * num, father), (2 * num + 1, mother)):
                if not ppid:
                    continue
                labels[slot_num] = self._chart_entry(ppid, page_dir)
                if ppid not in seen:          # pedigree collapse: show, don't re-walk
                    seen.add(ppid)
                    queue.append((slot_num, ppid))
        return labels

    def _build_family_wings(self, pid: str, page_dir: Path) -> dict:
        """Spouse(s) and children for the person-page family chart (the win-1
        extension of the ancestor pedigree), as two lists of `_chart_entry`
        dicts, keyed 'spouses' / 'children'.

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
        restricted) claim behind the edge. `--linked` shows every edge."""

        def collect(rel: str) -> list[dict]:
            out: list[dict] = []
            for r in self.conn.execute(
                'SELECT DISTINCT other_id FROM relationships WHERE person_id = ? AND rel = ? '
                'ORDER BY other_id', (pid, rel),
            ):
                other = r['other_id']
                if other == pid:
                    continue
                if not self.linked:
                    ometa = self.person_meta.get(other)
                    if (ometa is None or self._person_is_redacted(ometa)
                            or not self._has_public_claim(pid, other)):
                        continue
                out.append(self._chart_entry(other, page_dir))
            return out

        return {'spouses': collect('spouse'), 'children': collect('child')}

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

        # Surname A-Z: group curated (non-redacted) people by surname initial.
        by_letter: dict[str, list[dict]] = {}
        for pid in self.person_pages:
            meta = self.person_meta[pid]
            name = meta['name'] or fmt_id_display(pid)
            surname = (meta['surname'] or name or '?').strip()
            letter = surname[:1].upper() if surname[:1].isalpha() else '#'
            by_letter.setdefault(letter, []).append(
                {'name': name, 'href': f'persons/{_page_filename(pid)}'})
        surnames = [
            {'letter': letter, 'people': sorted(by_letter[letter], key=lambda p: p['name'].lower())}
            for letter in sorted(by_letter)
        ]

        _body, entries = self._read_discoveries()
        discoveries = [self._markup(_prose_to_html(chunk, render, embed, drop_private=not self.linked))
                       for chunk in entries]

        sources = sorted(
            ({'title': self.source_meta[sid]['title'] or fmt_id_display(sid),
              'href': f'sources/{_page_filename(sid)}'} for sid in self.source_pages),
            key=lambda s: s['title'].lower())
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
        home_md = self.archive_root / 'notes' / 'home.md'
        if home_md.is_file():
            try:
                body = (read_record(home_md).get('body') or '').strip()
                if body:
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
            except Exception:  # noqa: BLE001 - a bad home.md just falls back to the default
                self.messages.append('WARNING: notes/home.md could not be read; using the default intro.')

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

        # Descendant explorer (M8.5): seed from the apex of the configured
        # root_person's line so the tree fans forward across the whole family.
        tree = None
        root_person = normalize_id(str(self.fha_config.get('root_person', '')))
        if root_person and root_person not in self.person_meta:
            self.messages.append(
                f"WARNING: fha.yaml root_person {fmt_id_display(root_person)} is not in the index; "
                "the home family tree was skipped. Check the id, or run `fha index` if it was just added."
            )
        if root_person and root_person in self.person_meta:
            apex = self._apex_ancestor(root_person)
            apex_meta = self.person_meta.get(apex)
            if not self.linked and apex_meta and self._person_is_redacted(apex_meta):
                apex_name = _LIVING_LABEL
            elif apex_meta and apex_meta['name']:
                apex_name = apex_meta['name']
            else:
                apex_name = fmt_id_display(apex)
            # Descendant explorer: keep the full lineage but render the first few
            # generations up front so a large family doesn't paint thousands of
            # nodes at once (the reader expands forward).
            # The tree "Home" button centers on the configured home person, or
            # the Ahnentafel root by default. In standalone mode a redacted
            # target (living/unknown/restricted) is dropped so the button
            # doesn't point at a suppressed node or leak its P-id.
            home_person = normalize_id(str(site_cfg.get('home_person') or '')) or root_person
            if not self.linked and home_person:
                home_meta = self.person_meta.get(home_person)
                if home_meta is None or self._person_is_redacted(home_meta):
                    home_person = None
            tree = self._make_tree_ctx(apex, 'descendants', None, page_dir,
                                       f'Descendants of {apex_name}', initial_depth=4,
                                       home_id=home_person)

        self._write_page(self.out_dir / 'index.html', 'index.html', {
            'surnames': surnames, 'discoveries': discoveries, 'sources': sources,
            'places': places, 'intro': intro, 'tree': tree, 'hero': hero, 'root_prefix': '.',
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
            '`python -m pip install jinja2`, then run `fha site` again.',
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
              '`python -m pip install pillow` for photos in the standalone site.', file=sys.stderr)
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
