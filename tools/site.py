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
`.cache/photos.sqlite` when present.

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
    _prose_to_html             - minimal stdlib markdown→HTML (no md library)
    _inline_html               - inline pass: links, [ID] tokens, **bold**
    _extract_section           - pull one `## Heading` section body from a record

  Dates
    _decade_header             - EDTF date → "1880s" decade label (timeline grouping)

  Image derivatives
    _PIL_AVAILABLE             - is Pillow importable?
    _make_derivative           - resized, EXIF-stripped JPEG/PNG copy (standalone)

  Paths / hrefs
    _rel_href                  - relative href from a page dir to a target file
    _page_filename             - id → 'p-xxx.html' / 's-xxx.html'
    _json_for_script           - JSON serialized safe for inline <script> embedding

  Interactive tree (M8.5)
    _apex_ancestor             - deepest ancestor of root_person (home-tree seed)
    _build_tree_data           - BFS relationships → neutral tree JSON + url + redaction
    _tree_node, _person_vitals - one redacted node; its birth/death labels
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
    _cmd_site, register, _standalone_main
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    Result,
    FhaConfigError,
    configure_utf8_stdout,
    fmt_id_display,
    id_type_of,
    is_genetic_parent_subtype,
    is_working_copy,
    load_fha_yaml,
    normalize_id,
    strip_link_wrapper,
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

# Ancestor pedigree depth on person pages (M8.5: "3 generations default" =
# subject + 2 parent hops). The home descendant explorer is uncapped (the
# vendored renderer collapses large trees on demand).
_PEDIGREE_GENERATIONS = 3

# Redaction display strings (M8 UX bar: redacted content is named, never a blank).
_LIVING_LABEL = 'Living Person'
_RESTRICTED_LABEL = 'Restricted - not included in this publication'

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

    Handles markdown links, archive citation tokens (`[[ID|display]]` /
    `[[name]]` / legacy `[ID]`, delegated to `render_token`, which already
    returns safe HTML), and `**bold**`. Every run of literal text between
    constructs is HTML-escaped, so a stray `<` in a biography can never inject
    markup. `render_token` is the only source of un-escaped HTML and it is fully
    under our control (it emits anchors and spans we build).

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
            raw_url = m.group('lurl')
            safe_scheme = raw_url.startswith(('http://', 'https://', 'mailto:')) or '://' not in raw_url
            if safe_scheme:
                href = html.escape(raw_url, quote=True)
                out.append(f'<a href="{href}">{_escape(m.group("ltext"))}</a>')
            else:
                out.append(_escape(m.group('ltext')))
        else:  # bold
            out.append(f'<strong>{_escape(m.group("bold"))}</strong>')
    out.append(_escape(text[pos:]))
    return ''.join(out)


_HEADING_RE = re.compile(r'^(#{1,6})\s+(.*)$')
_LIST_RE = re.compile(r'^\s*[-*]\s+(.*)$')


def _prose_to_html(text: str, render_token) -> str:
    """Convert a simple markdown block to HTML using only the stdlib.

    The profile prose format is deliberately simple (TOOLING §12: "headings,
    bold, lists, links"), so a full markdown library is unwarranted. We split
    on blank lines into blocks; a block is a heading, a bullet list, or a
    paragraph. Inline formatting (links, tokens, bold) is applied per line via
    `_inline_html`. Headings below the page H1 render as `<h3>` so they sit
    under the section's own `<h2>` ("Biography", "Stories") without competing
    with it.
    """
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

def _make_derivative(src: Path, dest: Path) -> bool:
    """Write a resized, EXIF-stripped copy of `src` to `dest`. True on success.

    Standalone snapshots must carry their own image derivatives so no full-res
    original - and none of its EXIF (camera, GPS, timestamps that could leak a
    living person's location) - ever leaves the archive (TOOLING §12). PIL drops
    metadata on a plain save; we additionally cap the longest edge at 1200px.

    Failure (a corrupt image, an unsupported format, a locked file) returns
    False so the caller can warn-and-continue per the M8 UX bar (c) rather than
    abort the whole build. Caller must ensure PIL is available before calling.
    """
    try:
        with Image.open(src) as im:
            im = im.convert('RGB') if im.mode not in ('RGB', 'L') else im
            im.thumbnail((_DERIVATIVE_MAX_PX, _DERIVATIVE_MAX_PX))
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Re-save without the original info dict, so no EXIF/GPS survives.
            im.save(dest)
        return True
    except Exception:
        return False


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
    ) -> None:
        self.conn = conn
        self.archive_root = archive_root
        self.fha_config = fha_config
        self.out_dir = out_dir
        self.linked = linked          # False = standalone (default, redacted)
        self.messages: list[str] = []

        self.persons_dir = out_dir / 'persons'
        self.sources_dir = out_dir / 'sources'
        self.places_dir = out_dir / 'places'
        self.media_dir = out_dir / 'media'
        self.data_dir = out_dir / 'data'       # neutral tree JSON artifacts (M8.5)
        self.vendor_dir = out_dir / 'vendor'    # vendored tree renderer + adapter (M8.5)

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
        self.restricted_claims: set[str] = set()         # claim ids withheld
        self.restricted_names: dict[str, set[str]] = {}   # pid → lowercased restricted variant values
        # Opened once in prepare() when the photo index is fresh, reused across
        # every person page, closed by run_site - so the photos-root freshness
        # walk happens once per build, not once per curated person.
        self.photos_conn: sqlite3.Connection | None = None
        # discoveries.md is read for both the discoveries page and the home
        # teaser; memoize so the file is parsed once per build.
        self._discoveries: tuple[str, list[str]] | None = None

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
                for row in self.conn.execute(
                    f"SELECT DISTINCT c.source_id FROM claims c "
                    f"JOIN claim_persons cp ON c.id = cp.claim_id "
                    f"WHERE cp.person_id IN ({placeholders}) AND c.source_id IS NOT NULL",
                    rp,
                ):
                    source_living.add(row['source_id'])

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

    def _person_is_redacted(self, row: sqlite3.Row) -> bool:
        """A person is redacted from standalone output when living/unknown
        (AGENTS.md; `unknown` is treated as living) or `restricted` (any value,
        SPEC §21 - a restricted person, like a living one, gets no page and is
        rendered as a redaction label)."""
        if row['id'] in self.restricted_persons:
            return True
        return (row['living'] or '') in ('true', 'unknown')

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
                # Exception: when the aliases table is absent (stale index) we
                # can't distinguish a non-record link from an unresolved living
                # person - redact in standalone mode rather than leak a name.
                if not self.linked and not self._alias_table_ok:
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
            return name
        if kind == 'S' and pid in self.source_meta:
            row = self.source_meta[pid]
            # Any source absent from source_pages (restricted, DNA, publication_ok=false,
            # or linked to a living person) renders as the redacted label in standalone.
            if not self.linked and pid not in self.source_pages:
                return f'<span class="redacted">{_RESTRICTED_LABEL}</span>'
            title = _escape(in_display or row['title'] or display)
            if pid in self.source_pages:
                href = html.escape(_rel_href(self.sources_dir / _page_filename(pid), page_dir), quote=True)
                return f'<a href="{href}">{title}</a>'
            return title
        if kind == 'L' and pid in self.place_meta:
            name = _escape(in_display or self.place_names.get(pid) or display)
            if pid in self.place_pages:
                href = html.escape(_rel_href(self.places_dir / _page_filename(pid), page_dir), quote=True)
                return f'<a href="{href}">{name}</a>'
            return name
        # Unresolved ID token - surfaced as the literal [X-xxxx] form, not hidden
        # (TOOLING §12 / BUILD M8.1; these are already lint errors).
        return f'<mark>[{_escape(display)}]</mark>'

    def _person_link(self, pid: str, page_dir: Path) -> str:
        """A bare person reference (not from prose) → link / redaction / name."""
        return self.render_token(fmt_id_display(pid), page_dir)

    def _source_link(self, sid: str, page_dir: Path) -> str:
        """A compact `[S-id]` citation chip for timelines and summary rows.

        Distinct from `render_token`'s prose handling, which renders an S-id as
        the source *title* so a sentence reads naturally. In a dense timeline or
        summary a short bracketed id is clearer, so this builds the chip
        directly - while honoring the same redaction and page-existence rules.
        """
        sid = normalize_id(sid)
        chip = f'[{_escape(fmt_id_display(sid))}]'
        row = self.source_meta.get(sid)
        if row is None:
            return f'<mark>{chip}</mark>'
        if not self.linked and self._source_is_redacted(row):
            return f'<span class="redacted">{_RESTRICTED_LABEL}</span>'
        if sid in self.source_pages:
            href = html.escape(_rel_href(self.sources_dir / _page_filename(sid), page_dir), quote=True)
            return f'<a href="{href}">{chip}</a>'
        return chip

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
            href = _rel_href(resolved, page_dir)
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
            })

        files = self._source_file_entries(sid, page_dir)

        ctx = {
            'display_id': fmt_id_display(sid), 'title': row['title'] or fmt_id_display(sid),
            'source_type': row['source_type'] or '', 'citation': citation,
            'date': row['date_edtf'] or '', 'repository': row['repository'] or '',
            'source_class': row['source_class'] or '', 'claims': claims, 'files': files,
        }
        self._write_page(self.sources_dir / _page_filename(sid), 'source.html',
                         {'source': ctx, 'root_prefix': '..'})

    def _source_file_entries(self, sid: str, page_dir: Path) -> list[dict]:
        """Build the file-list entries for a source page, creating standalone
        image derivatives as needed."""
        entries: list[dict] = []
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
        return entries

    # - person page (M8.2) -

    def build_person_page(self, pid: str) -> None:
        """Render one curated person page (TOOLING §12 / M8.2)."""
        row = self.person_meta[pid]
        page_dir = self.persons_dir

        summary = self._person_summary(pid, page_dir)
        biography_html, stories_html = self._person_prose(row, page_dir)
        timeline = self._person_timeline(pid, page_dir)
        sources = self._person_sources(pid, page_dir)
        family = self._person_family(pid, page_dir)
        photos = self._person_photos(pid, page_dir)
        name = row['name'] or fmt_id_display(pid)
        tree = self._make_tree_ctx(pid, 'ancestors', _PEDIGREE_GENERATIONS - 1, page_dir,
                                   f'Ancestor pedigree of {name}')

        ctx = {
            'display_id': fmt_id_display(pid), 'name': name,
            'summary': summary,
            'biography_html': self._markup(biography_html) if biography_html else None,
            'stories_html': self._markup(stories_html) if stories_html else None,
            'timeline': timeline, 'sources': sources, 'family': family, 'photos': photos,
        }
        self._write_page(self.persons_dir / _page_filename(pid), 'person.html',
                         {'person': ctx, 'tree': tree, 'root_prefix': '..'})

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
            rows = [r for r in rows
                    if (r['source_id'] is None or r['source_id'] in self.source_pages)
                    and normalize_id(str(r['id'])) not in self.restricted_claims]
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
                })
        return summary

    def _person_prose(self, row: sqlite3.Row, page_dir: Path) -> tuple[str, str]:
        """Biography and Stories HTML, read from the person `.md` body."""
        try:
            rec = read_record(self.archive_root / row['path'])
        except Exception as e:
            self.messages.append(f'WARNING: could not read {row["path"]} ({e}); skipping its prose.')
            return '', ''
        render = lambda tok, disp=None: self.render_token(tok, page_dir, disp)  # noqa: E731 - tiny closure
        bio = _extract_section(rec['body'], 'Biography')
        biography_html = _prose_to_html(bio, render) if bio else ''
        stories_html = _prose_to_html(rec['stories'], render) if rec['stories'] else ''
        return biography_html, stories_html

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
        # Standalone: omit events backed only by withheld sources (same rule as
        # summary vitals), and omit a restricted claim regardless of its source.
        if not self.linked:
            rows = [r for r in rows
                    if (r['source_id'] is None or r['source_id'] in self.source_pages)
                    and normalize_id(str(r['id'])) not in self.restricted_claims]
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
        """Sources citing the person, grouped by source_type (TOOLING §12 - the
        same two-table UNION as `fha views sources-index`)."""
        status_filter = '' if self.linked else "AND c.status IN ('accepted','needs-review')"
        rows = self.conn.execute(
            f'SELECT DISTINCT c.source_id FROM claim_persons cp JOIN claims c ON cp.claim_id = c.id '
            f'WHERE cp.person_id = ? {status_filter} '
            'UNION SELECT DISTINCT source_id FROM source_people WHERE person_id = ?',
            (pid, pid),
        ).fetchall()
        by_type: dict[str, list[str]] = {}
        for r in rows:
            sid = r[0]
            if sid not in self.source_meta:
                continue
            # Standalone: only list sources that actually have a public page.
            if not self.linked and sid not in self.source_pages:
                continue
            st = self.source_meta[sid]['source_type'] or 'other'
            by_type.setdefault(st, []).append(self._source_link(sid, page_dir))
        return [
            {'source_type': st, 'sources': [self._markup(s) for s in sorted(by_type[st])]}
            for st in sorted(by_type)
        ]

    def _has_public_claim(self, pid1: str, pid2: str) -> bool:
        """Return True if the two persons share at least one accepted/needs-review claim
        backed by a public (non-withheld) source or no source at all.

        Used in standalone mode to suppress relationship edges whose only evidence comes
        from restricted, DNA, or living-linked sources."""
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
            if r['source_id'] is None or r['source_id'] in self.source_pages:
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
            if person and (person['living'] or '') in ('true', 'unknown'):
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
            href = _rel_href(resolved, page_dir)
            return {'href': href, 'full_href': href, 'caption': caption}
        if not _PIL_AVAILABLE:
            return None
        dest = self._media_dest(row['path'], 'people')
        if not _make_derivative(resolved, dest):
            self.messages.append(f'WARNING: could not build a web image for {row["path"]} (omitted from photo strip)')
            return None
        href = _rel_href(dest, page_dir)
        return {'href': href, 'full_href': href, 'caption': caption}

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
            claim_rows = [c for c in claim_rows
                          if (c['source_id'] is None or c['source_id'] in self.source_pages)
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
        content_html = _prose_to_html(body, render) if body else ''
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
            # Standalone: skip dates from restricted claims or withheld sources.
            if not self.linked and normalize_id(str(r['id'])) in self.restricted_claims:
                continue
            if not self.linked and r['source_id'] is not None and r['source_id'] not in self.source_pages:
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
                'vitals': self._person_vitals(pid), 'url': url}

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
                    if other not in self.person_pages:
                        continue
                    if not self._has_public_claim(cur, other):
                        continue
                # The edge's nature (SPEC §12.2): a non-genetic parent/child bond
                # (adoptive, step, foster, guardian, …) draws distinctly from the
                # genetic line. Unset/legacy subtypes default to genetic.
                subtype = (r['subtype'] or '').strip().lower() or None
                edges.append({
                    'type': rel, 'from': fmt_id_display(cur), 'to': fmt_id_display(other),
                    'claim_id': fmt_id_display(r['claim_id']) if r['claim_id'] else None,
                    'subtype': subtype,
                    'genetic': is_genetic_parent_subtype(subtype),
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

    def _make_tree_ctx(self, seed: str, mode: str, max_hops: int | None,
                       page_dir: Path, caption: str, *, initial_depth: int | None = None) -> dict | None:
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
                'initial_depth': initial_depth}

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

    # - index / home page (M8.4) -

    def build_index_page(self) -> None:
        """The home page (TOOLING §12 / M8.4): a surname A-Z index of people and
        a recent-discoveries teaser (last five entries), plus source and place
        navigation so every generated page is reachable. The surname index is
        built from `person_pages`, which already excludes redacted persons under
        standalone - so the home page never lists or links a living person."""
        page_dir = self.out_dir
        render = lambda tok, disp=None: self.render_token(tok, page_dir, disp)  # noqa: E731

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
        discoveries = [self._markup(_prose_to_html(chunk, render)) for chunk in entries]

        sources = sorted(
            ({'title': self.source_meta[sid]['title'] or fmt_id_display(sid),
              'href': f'sources/{_page_filename(sid)}'} for sid in self.source_pages),
            key=lambda s: s['title'].lower())
        places = sorted(
            ({'name': self.place_meta[lid]['name'] or fmt_id_display(lid),
              'href': f'places/{_page_filename(lid)}'} for lid in self.place_pages),
            key=lambda p: p['name'].lower())
        intro = (
            'A safe-to-share snapshot of this family archive.' if not self.linked
            else 'Local developer preview (linked mode - not redacted, do not share).'
        )

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
            tree = self._make_tree_ctx(apex, 'descendants', None, page_dir,
                                       f'Descendants of {apex_name}', initial_depth=4)

        self._write_page(self.out_dir / 'index.html', 'index.html', {
            'surnames': surnames, 'discoveries': discoveries, 'sources': sources,
            'places': places, 'intro': intro, 'tree': tree, 'root_prefix': '.',
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
            full = {
                'site_title': 'Family History Archive',
                'footer_note': (
                    'Generated by fha site. Living people and restricted material are excluded from this snapshot.'
                    if not self.linked else
                    'Generated by fha site (linked preview - unredacted; do not publish).'
                ),
            }
            full.update(ctx)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tmpl.render(**full), encoding='utf-8')
        except Exception as e:  # noqa: BLE001 - one page's failure must not abort the build
            self.messages.append(f'WARNING: could not generate {path.name} ({e}); skipped.')

    def run(self) -> int:
        """Generate the whole site. Returns the number of pages written."""
        self._reset_output()
        self._copy_vendor()
        for sid in sorted(self.source_pages):
            self.build_source_page(sid)
        for pid in sorted(self.person_pages):
            self.build_person_page(pid)
        for lid in sorted(self.place_pages):
            self.build_place_page(lid)
        self.build_discoveries_page()
        self.build_index_page()
        # source + person + place pages, plus discoveries.html and index.html
        return len(self.source_pages) + len(self.person_pages) + len(self.place_pages) + 2

    def _reset_output(self) -> None:
        """Clear only the subtrees this tool owns, so a rebuild drops pages for
        records that became redacted (idempotent regeneration - TOOLING §12)
        without disturbing anything else a human keeps in the output directory.

        Standalone builds raise OSError if a subtree cannot be removed - leaving a
        previously generated page for a now-redacted person would be a privacy leak.
        Linked (dev preview) mode silently ignores removal failures."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for d in (self.persons_dir, self.sources_dir, self.places_dir, self.media_dir,
                  self.data_dir, self.vendor_dir):
            if d.exists():
                shutil.rmtree(d, ignore_errors=self.linked)
        for f in ('index.html', 'discoveries.html'):
            target = self.out_dir / f
            if target.exists():
                target.unlink()


# ── Core / CLI ──────────────────────────────────────────────────────────────

def _site_payload(
    archive_root: Path,
    out_dir: Path,
    *,
    linked: bool = False,
    dry_run: bool = False,
) -> dict:
    """Build the site and return a result dict.

    Returns {'status', 'messages', 'out_dir', 'pages'} where status is one of:
      'no-jinja'    - Jinja2 not installed (CLI prints an install hint)
      'no-index'    - index absent/unreadable/stale (open_index_db already explained;
                      standalone builds refuse a stale index - run `fha index` first)
      'bad-config'  - fha.yaml is malformed (message carries the detail)
      'bad-output'  - output dir would clobber archive content (CLI explains)
      'dry-run'     - would build N pages; nothing written
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

    # Standalone builds refuse a stale index to avoid publishing redacted persons whose
    # living flag was changed since the last `fha index` run.  Linked (dev preview)
    # mode only warns - a slightly stale preview beats no preview.
    conn = open_index_db(archive_root, _REQUIRED_TABLES, strict=not linked)
    if conn is None:
        return {'status': 'no-index', 'messages': [], 'out_dir': out_dir, 'pages': 0}

    builder = _SiteBuilder(conn, archive_root, fha_config, out_dir, linked=linked)
    try:
        builder.prepare()
        if dry_run:
            return {
                'status': 'dry-run', 'messages': builder.messages, 'out_dir': out_dir,
                'pages': (len(builder.source_pages) + len(builder.person_pages)
                          + len(builder.place_pages) + 2),
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
) -> Result:
    """Library entry point. Build the site and return a Result.

    `data` is the `_site_payload` dict ({'status', 'messages', 'out_dir',
    'pages'}); Result exposes dict-style access (_lib.py), so callers keep
    reading `result['status']` / `result['pages']` unchanged.  A real build lists
    the written output directory in `changed`; a --dry-run (status 'dry-run')
    writes nothing and leaves `changed` empty.
    """
    if is_working_copy(archive_root):
        return Result(
            ok=False,
            exit_code=EXIT_CLEAN,
            data={'status': 'working-copy', 'out_dir': str(out_dir), 'pages': [], 'messages': []},
        ).add(
            'warning',
            'fha site is not available in working-copy mode - '
            'the photo and document files are on the main machine. '
            'Build the site there.',
        )
    payload = _site_payload(archive_root, out_dir, linked=linked, dry_run=dry_run)
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
    both before any write. The default `.cache/site/` is always safe.
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
            'delete your records. Pick a separate folder, e.g. `--out .cache/site` (the default).'
        )
    # A different archive (its own fha.yaml + record tree) must not be clobbered.
    if (out_dir / 'fha.yaml').exists():
        return (
            f'Refusing to build the site into {out_dir}: it looks like another archive '
            '(it has an fha.yaml). Choose an empty or site-only folder, e.g. the default `.cache/site`.'
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
                'Choose a separate folder, e.g. the default `.cache/site`.'
            )
    return None


def _display_path(p: Path, archive_root: Path) -> str:
    try:
        return str(p.relative_to(archive_root))
    except ValueError:
        return str(p)


def _cmd_site(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    out_dir = Path(getattr(args, 'out', None) or Path('.cache') / 'site')
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

    mode = 'linked preview' if getattr(args, 'linked', False) else 'standalone snapshot'
    where = _display_path(result['out_dir'], archive_root)
    if status == 'dry-run':
        print(f'(dry run - no files written) Would build {result["pages"]} pages ({mode}) in {where}')
        return EXIT_WARNINGS if result['messages'] else EXIT_CLEAN

    print(f'Site built: {result["pages"]} pages ({mode}) in {where}')
    if not getattr(args, 'linked', False) and not _PIL_AVAILABLE:
        print('Note: Pillow is not installed, so images were omitted. Install it with '
              '`python -m pip install pillow` for photos in the standalone site.', file=sys.stderr)
    return EXIT_WARNINGS if result['messages'] else EXIT_CLEAN


def _add_site_args(p: argparse.ArgumentParser) -> None:
    p.add_argument('--out', metavar='PATH', dest='out',
                   help='Output directory (default: .cache/site/ under the archive root).')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--standalone', dest='linked', action='store_false',
                      help='Self-contained, redacted snapshot safe to share (default).')
    mode.add_argument('--linked', dest='linked', action='store_true',
                      help='Local developer preview: real paths, no copies, no redaction.')
    p.set_defaults(linked=False)
    p.add_argument('--dry-run', action='store_true', dest='dry_run',
                   help='Report how many pages would be built without writing anything.')
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p.add_argument('--spec-root', metavar='PATH', help='Spec docs root (accepted for CLI consistency).')


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = subs.add_parser(
        'site',
        help='Generate the static HTML family explorer (standalone snapshot or linked preview).',
        description=(
            'Render the archive as a browsable static website that opens from file://.\n'
            '--standalone (default) is the redacted, self-contained snapshot safe to share;\n'
            '--linked is an unredacted local preview for developers (TOOLING §12).'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_site_args(p)
    p.set_defaults(func=_cmd_site)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha site', description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_site_args(parser)
    parser.set_defaults(func=_cmd_site)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
