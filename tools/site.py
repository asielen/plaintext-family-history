#!/usr/bin/env python3
"""
site.py — fha site: the static-HTML family explorer (TOOLING §12).

  fha site [--out PATH] [--standalone | --linked] [--dry-run] [--root PATH]

ARCHITECTURE OVERVIEW
----------------------
`fha site` renders the whole archive as a browsable, fully-relative-link
website that opens straight from `file://` — no server, no CDN, no JS
framework. It is a *snapshot*, not a live view: structured data is read from
`.cache/index.sqlite` (so the site is exactly as fresh as the last
`fha index`), prose (biography, Stories) is read from the curated person
`.md` file, the citation text is read from the source `.md` frontmatter
(the index does not carry it), and the photo strip is read from
`.cache/photos.sqlite` when present.

Two build modes, one generator:
  - `--standalone` (default): the safe-to-share snapshot. Living/unknown
    persons get no page and render as "Living Person"; restricted, DNA, and
    `rights.publication_ok: false` sources get no page and render as
    "Restricted — not included in this publication"; image assets become
    web-optimized, EXIF-stripped derivatives copied into `site/media/` so the
    snapshot depends on nothing outside itself.
  - `--linked`: a fast *local* developer preview. Real archive paths (no
    copies), no redaction guarantees. Never hand this folder to anyone.

This file ships milestones M8.1 (foundations: query layer, Jinja2, source
page) and M8.2 (the curated person page). Place, discoveries, home-page
enrichment, and the interactive tree are later phases (M8.3-M8.5); a minimal
people/sources index page is generated now so navigation works and links
never dangle.

WHY A LIBRARY FUNCTION (`run_site`): mirrors packet/report — a testable
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
    _escape                    — html.escape shorthand
    _prose_to_html             — minimal stdlib markdown→HTML (no md library)
    _inline_html               — inline pass: links, [ID] tokens, **bold**
    _extract_section           — pull one `## Heading` section body from a record

  Dates
    _decade_header             — EDTF date → "1880s" decade label (timeline grouping)

  Image derivatives
    _PIL_AVAILABLE             — is Pillow importable?
    _make_derivative           — resized, EXIF-stripped JPEG/PNG copy (standalone)

  Paths / hrefs
    _rel_href                  — relative href from a page dir to a target file
    _page_filename             — id → 'p-xxx.html' / 's-xxx.html'

  Builder
    _SiteBuilder               — holds conn, mode, maps, page sets, jinja env
      .prepare                 — load persons/sources, decide which pages exist
      .render_token            — one [ID] token → HTML (link / redaction / mark)
      .build_source_page       — M8.1 source page
      .build_person_page       — M8.2 person page
      .build_index_page        — minimal people+sources landing page
      .run                     — orchestrate: prepare, build all pages, write

  Core / CLI
    run_site                   — library entry point
    _cmd_site, register, _standalone_main
"""

from __future__ import annotations

import argparse
import datetime
import html
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    TOKEN_RE,
    FhaConfigError,
    configure_utf8_stdout,
    fmt_id_display,
    id_type_of,
    load_fha_yaml,
    normalize_id,
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

try:  # Pillow is OPTIONAL — standalone image derivatives use it when present.
    from PIL import Image
    _PIL_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - environment-dependent
    Image = None  # type: ignore[assignment]
    _PIL_AVAILABLE = False


_REQUIRED_TABLES = (
    'persons', 'sources', 'claims', 'claim_persons', 'source_files',
    'source_people', 'relationships', 'places', 'person_files',
)

_IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.heic', '.bmp', '.gif'}

# The largest edge (px) a standalone derivative is resized to (TOOLING §12).
_DERIVATIVE_MAX_PX = 1200

# Redaction display strings (M8 UX bar: redacted content is named, never a blank).
_LIVING_LABEL = 'Living Person'
_RESTRICTED_LABEL = 'Restricted — not included in this publication'

# Summary-block label per vital claim type (M8.2 "summary block (accepted vitals)").
_VITAL_LABELS = {
    'birth': 'Born', 'death': 'Died', 'marriage': 'Married',
    'baptism': 'Baptized', 'burial': 'Buried',
}
_VITAL_ORDER = ['birth', 'baptism', 'marriage', 'death', 'burial']

# Friends & Family grouping, in display order (TOOLING §12).
_FAMILY_GROUPS = [
    ('parent', 'Parents'),
    ('spouse', 'Spouses'),
    ('child', 'Children'),
    ('sibling', 'Siblings'),
    ('friend', 'Friends'),
    ('associate', 'Associates'),
    ('neighbor', 'Neighbors'),
]


def _today() -> str:
    return datetime.date.today().isoformat()


# ── Prose / HTML ────────────────────────────────────────────────────────────

def _escape(text: str) -> str:
    """html.escape, never quoting — we only emit text into element bodies here."""
    return html.escape(text, quote=False)


# Inline constructs, tried left to right. A markdown link `[text](url)` is
# matched before an `[ID]` token so a token never half-matches a link; bold is
# last. Anything not matched is literal text and gets escaped.
_INLINE_RE = re.compile(
    r'\[(?P<ltext>[^\]]+)\]\((?P<lurl>[^)\s]+)\)'                 # [text](url)
    r'|\[(?P<token>[PSCLH]-[0-9a-hjkmnp-tv-z]{10})\]'            # [ID] token
    r'|\*\*(?P<bold>.+?)\*\*',                                    # **bold**
    re.I,
)


def _inline_html(text: str, render_token) -> str:
    """Render one block of inline prose to HTML.

    Handles markdown links, archive `[ID]` tokens (delegated to `render_token`,
    which already returns safe HTML), and `**bold**`. Every run of literal text
    between constructs is HTML-escaped, so a stray `<` in a biography can never
    inject markup. `render_token` is the only source of un-escaped HTML and it
    is fully under our control (it emits anchors and spans we build).
    """
    out: list[str] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        out.append(_escape(text[pos:m.start()]))
        pos = m.end()
        if m.group('token'):
            out.append(render_token(m.group('token')))
        elif m.group('ltext') is not None:
            href = html.escape(m.group('lurl'), quote=True)
            out.append(f'<a href="{href}">{_escape(m.group("ltext"))}</a>')
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
    imported — tools never import tools (TOOLING §15).
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
    original — and none of its EXIF (camera, GPS, timestamps that could leak a
    living person's location) — ever leaves the archive (TOOLING §12). PIL drops
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
    site on C:\\) — fall back to a `file://` absolute URI so the link still
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


# ── Builder ─────────────────────────────────────────────────────────────────

class _SiteBuilder:
    """Holds the shared state for one site build and renders every page.

    Constructed once per `run_site`. `prepare()` loads the person/source
    metadata and decides — once, up front — which person and source pages will
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

        self.person_meta: dict[str, sqlite3.Row] = {}
        self.source_meta: dict[str, sqlite3.Row] = {}
        self.place_meta: dict[str, sqlite3.Row] = {}
        self.place_names: dict[str, str] = {}   # id → display name (token rendering)
        self.person_pages: set[str] = set()   # normalized pids that get a page
        self.source_pages: set[str] = set()   # normalized sids that get a page
        self.place_pages: set[str] = set()    # normalized lids that get a page
        # Opened once in prepare() when the photo index is fresh, reused across
        # every person page, closed by run_site — so the photos-root freshness
        # walk happens once per build, not once per curated person.
        self.photos_conn: sqlite3.Connection | None = None

        if jinja2 is not None:
            self.env = jinja2.Environment(
                loader=jinja2.FileSystemLoader(str(Path(__file__).parent / 'templates')),
                autoescape=jinja2.select_autoescape(['html']),
            )
        else:  # pragma: no cover - guarded earlier in run_site
            self.env = None

    # — preparation —

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

        for sid, row in self.source_meta.items():
            if self.linked or not self._source_is_redacted(row):
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

    def _open_photos(self) -> None:
        """Open the photo index once if it is fresh, for the person photo strips.

        The freshness check (`photoindex_status`) walks the whole photos root,
        so it must run once per build — never once per person. An absent, stale,
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
        wikitree): absent → publishable, explicit false → withheld."""
        if (row['restricted'] or 0):
            return True
        if (row['source_type'] or '') == 'dna':
            return True
        pub = row['publication_ok']
        return pub is not None and int(pub) == 0

    def _person_is_redacted(self, row: sqlite3.Row) -> bool:
        """Living and unknown-living persons are redacted from standalone output
        (AGENTS.md privacy rule; `unknown` is treated as living)."""
        return (row['living'] or '') in ('true', 'unknown')

    # — token rendering —

    def render_token(self, token: str, page_dir: Path) -> str:
        """Render one `[ID]` token to HTML, relative to the page being built.

        P-id → link to the person page when one exists; "Living Person" when the
        person is redacted under standalone; otherwise the plain (escaped) name.
        S-id → link to the source page, or "Restricted — not included…" when
        withheld. L-id → link to the place page (places are never redacted).
        Any token whose target is absent from the index renders highlighted —
        `<mark>[X-xxxx]</mark>` — exactly as TOOLING §12 / BUILD M8.1 specify
        (these are already lint errors, surfaced rather than hidden).
        """
        pid = normalize_id(token)
        kind = id_type_of(pid)
        display = fmt_id_display(pid)

        if kind == 'P' and pid in self.person_meta:
            row = self.person_meta[pid]
            if not self.linked and self._person_is_redacted(row):
                return f'<span class="redacted">{_LIVING_LABEL}</span>'
            name = _escape(row['name'] or display)
            if pid in self.person_pages:
                href = html.escape(_rel_href(self.persons_dir / _page_filename(pid), page_dir), quote=True)
                return f'<a href="{href}">{name}</a>'
            return name
        if kind == 'S' and pid in self.source_meta:
            row = self.source_meta[pid]
            if not self.linked and self._source_is_redacted(row):
                return f'<span class="redacted">{_RESTRICTED_LABEL}</span>'
            title = _escape(row['title'] or display)
            if pid in self.source_pages:
                href = html.escape(_rel_href(self.sources_dir / _page_filename(pid), page_dir), quote=True)
                return f'<a href="{href}">{title}</a>'
            return title
        if kind == 'L' and pid in self.place_meta:
            name = _escape(self.place_names.get(pid) or display)
            if pid in self.place_pages:
                href = html.escape(_rel_href(self.places_dir / _page_filename(pid), page_dir), quote=True)
                return f'<a href="{href}">{name}</a>'
            return name
        # Unresolved token — surfaced as the literal [X-xxxx] form, not hidden
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
        directly — while honoring the same redaction and page-existence rules.
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

    # — assets —

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
            original stays in the archive (originals never leave — TOOLING §12).
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

    def _standalone_image_entry(self, sid: str, asset_rel: str, role: str | None, page_dir: Path) -> dict:
        """Create the media derivative for a standalone image asset and return
        its file entry. Split from `_file_entry` because it needs the source id
        for the media subfolder and may emit a warning into `self.messages`."""
        resolved = resolve_path(asset_rel, self.fha_config, self.archive_root)
        dest = self.media_dir / normalize_id(sid) / (Path(asset_rel).stem + '.jpg')
        if _make_derivative(resolved, dest):
            href = _rel_href(dest, page_dir)
            return {'label': Path(asset_rel).name, 'note': f'role: {role}' if role else None,
                    'link_href': href, 'thumb_href': href}
        self.messages.append(f'WARNING: could not build a web image for {asset_rel} (skipped, build continues)')
        return {'label': Path(asset_rel).name, 'note': 'image could not be processed', 'link_href': None, 'thumb_href': None}

    # — source page (M8.1) —

    def build_source_page(self, sid: str) -> None:
        """Render one source page: citation, metadata, claims table, files.

        Wrapped so a single malformed source never aborts the build — its page
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

        claims = []
        for c in self.conn.execute(
            'SELECT id, type, value, date_edtf, place_id, place_text, status FROM claims '
            'WHERE source_id = ? ORDER BY '
            "CASE WHEN date_min IS NULL OR date_min = '' THEN 1 ELSE 0 END, date_min ASC",
            (sid,),
        ):
            person_rows = self.conn.execute(
                'SELECT person_id FROM claim_persons WHERE claim_id = ? ORDER BY position', (c['id'],)
            ).fetchall()
            persons_html = ', '.join(self._person_link(p['person_id'], page_dir) for p in person_rows)
            claims.append({
                'type': c['type'], 'value': c['value'], 'date': c['date_edtf'] or '',
                'place': self._place_label(c['place_text'], c['place_id']),
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
            entry = self._file_entry(f['path'], f['role'], page_dir)
            if entry is None:   # standalone image needing a derivative
                entry = self._standalone_image_entry(sid, f['path'], f['role'], page_dir)
            entries.append(entry)
        return entries

    # — person page (M8.2) —

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

        ctx = {
            'display_id': fmt_id_display(pid), 'name': row['name'] or fmt_id_display(pid),
            'summary': summary,
            'biography_html': self._markup(biography_html) if biography_html else None,
            'stories_html': self._markup(stories_html) if stories_html else None,
            'timeline': timeline, 'sources': sources, 'family': family, 'photos': photos,
        }
        self._write_page(self.persons_dir / _page_filename(pid), 'person.html',
                         {'person': ctx, 'root_prefix': '..'})

    def _person_summary(self, pid: str, page_dir: Path) -> list[dict]:
        """Accepted vital claims as the summary block (birth/death/marriage/…)."""
        rows = self.conn.execute(
            "SELECT type, value, date_edtf, place_id, place_text, source_id FROM claims c "
            "JOIN claim_persons cp ON c.id = cp.claim_id "
            "WHERE cp.person_id = ? AND c.status = 'accepted' AND c.type IN ('birth','death','marriage','baptism','burial')",
            (pid,),
        ).fetchall()
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
                    'place': self._place_label(r['place_text'], r['place_id']),
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
        render = lambda tok: self.render_token(tok, page_dir)  # noqa: E731 - tiny closure
        bio = _extract_section(rec['body'], 'Biography')
        biography_html = _prose_to_html(bio, render) if bio else ''
        stories_html = _prose_to_html(rec['stories'], render) if rec['stories'] else ''
        return biography_html, stories_html

    def _person_timeline(self, pid: str, page_dir: Path) -> list[dict]:
        """Accepted + needs-review claims, grouped by decade (TOOLING §12 — the
        same query and shape as `fha views timeline`'s main chronology; suggested
        claims are excluded from the published timeline)."""
        rows = self.conn.execute(
            "SELECT DISTINCT c.date_edtf, c.date_min, c.type, c.value, c.place_id, c.place_text, c.source_id "
            "FROM claim_persons cp JOIN claims c ON cp.claim_id = c.id "
            "WHERE cp.person_id = ? AND c.status IN ('accepted','needs-review') "
            "ORDER BY CASE WHEN c.date_min IS NULL OR c.date_min = '' THEN 1 ELSE 0 END, c.date_min ASC",
            (pid,),
        ).fetchall()
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
                'place': self._place_label(r['place_text'], r['place_id']),
                'source_html': self._markup(self._source_link(r['source_id'], page_dir)) if r['source_id'] else '',
            })
        if entries:
            groups.append({'decade': None if current == '\x00' else current, 'entries': entries})
        return groups

    def _person_sources(self, pid: str, page_dir: Path) -> list[dict]:
        """Sources citing the person, grouped by source_type (TOOLING §12 — the
        same two-table UNION as `fha views sources-index`)."""
        rows = self.conn.execute(
            'SELECT DISTINCT c.source_id FROM claim_persons cp JOIN claims c ON cp.claim_id = c.id '
            'WHERE cp.person_id = ? '
            'UNION SELECT DISTINCT source_id FROM source_people WHERE person_id = ?',
            (pid, pid),
        ).fetchall()
        by_type: dict[str, list[str]] = {}
        for r in rows:
            sid = r[0]
            if sid not in self.source_meta:
                continue
            st = self.source_meta[sid]['source_type'] or 'other'
            by_type.setdefault(st, []).append(self._source_link(sid, page_dir))
        return [
            {'source_type': st, 'sources': [self._markup(s) for s in sorted(by_type[st])]}
            for st in sorted(by_type)
        ]

    def _person_family(self, pid: str, page_dir: Path) -> list[dict]:
        """Friends & Family from the relationships edges, grouped by relation."""
        rows = self.conn.execute(
            'SELECT DISTINCT rel, other_id FROM relationships WHERE person_id = ?', (pid,)
        ).fetchall()
        by_rel: dict[str, list[str]] = {}
        for r in rows:
            by_rel.setdefault(r['rel'], []).append(self._person_link(r['other_id'], page_dir))
        groups = []
        for rel, label in _FAMILY_GROUPS:
            if rel in by_rel:
                groups.append({'label': label, 'members': [self._markup(m) for m in sorted(by_rel[rel])]})
        return groups

    def _person_photos(self, pid: str, page_dir: Path) -> list[dict]:
        """Photo strip from `.cache/photos.sqlite` (`photo_people`), one entry
        per variation group. Omitted silently when the photo index is absent or
        stale (`self.photos_conn` None) — it is an optional enrichment, never a
        build blocker. Uses the connection opened once in `prepare()`."""
        if self.photos_conn is None:
            return []
        try:
            rows = self.photos_conn.execute(
                'SELECT DISTINCT ph.group_id, ph.path, ph.caption, ph.is_primary '
                'FROM photo_people pp JOIN photos ph ON pp.path = ph.path '
                'WHERE pp.person_ref = ?',
                (pid,),
            ).fetchall()
        except sqlite3.DatabaseError:
            return []
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
        dest = self.media_dir / 'people' / (Path(row['path']).stem + '.jpg')
        if not _make_derivative(resolved, dest):
            self.messages.append(f'WARNING: could not build a web image for {row["path"]} (omitted from photo strip)')
            return None
        href = _rel_href(dest, page_dir)
        return {'href': href, 'full_href': href, 'caption': caption}

    # — place page (M8.3) —

    def build_place_page(self, lid: str) -> None:
        """Render one place page (TOOLING §12 / M8.3): name, coords (a map *URL*,
        no embedded map dependency), dated `history:`, claims naming the place,
        contained micro-places (`within:` children), and the people most often
        associated with it. People links follow the standard redaction rule, and
        the people-frequency list omits redacted persons entirely so a standalone
        place page never links to — or even names — a living person."""
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

        claim_rows = self.conn.execute(
            "SELECT id, type, value, date_edtf, date_min, source_id FROM claims "
            "WHERE place_id = ? AND status IN ('accepted','needs-review') "
            "ORDER BY CASE WHEN date_min IS NULL OR date_min = '' THEN 1 ELSE 0 END, date_min ASC",
            (lid,),
        ).fetchall()
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

    # — discoveries page (M8.3) —

    def build_discoveries_page(self) -> None:
        """Render `notes/discoveries.md` as the discoveries page (TOOLING §12 /
        M8.3). P-id/S-id mentions are linked (and redacted) by the shared token
        renderer, so a living person named in a discovery never leaks here under
        standalone. A missing or empty file yields a plain "nothing logged yet"
        page rather than a broken link from the home teaser."""
        body, _entries = self._read_discoveries()
        page_dir = self.out_dir
        render = lambda tok: self.render_token(tok, page_dir)  # noqa: E731
        content_html = _prose_to_html(body, render) if body else ''
        self._write_page(self.out_dir / 'discoveries.html', 'discoveries.html', {
            'content_html': self._markup(content_html) if content_html else None,
            'root_prefix': '.',
        })

    def _read_discoveries(self) -> tuple[str, list[str]]:
        """Read notes/discoveries.md and return (body_without_leading_H1,
        recent_entry_chunks). An entry is a `##`/`###` section or a top-level
        `-` bullet — the dated, ref-carrying shape TOOLING §15a appends. The
        schema is loose by design, so this is tolerant: no recognizable entries
        means an empty teaser, never an error. The last five chunks (most
        recently appended) are returned for the home-page teaser."""
        path = self.archive_root / 'notes' / 'discoveries.md'
        try:
            text = path.read_text(encoding='utf-8')
        except OSError:
            return '', []
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
        return body, chunks[-5:]

    # — index / home page (M8.4) —

    def build_index_page(self) -> None:
        """The home page (TOOLING §12 / M8.4): a surname A-Z index of people and
        a recent-discoveries teaser (last five entries), plus source and place
        navigation so every generated page is reachable. The surname index is
        built from `person_pages`, which already excludes redacted persons under
        standalone — so the home page never lists or links a living person."""
        page_dir = self.out_dir
        render = lambda tok: self.render_token(tok, page_dir)  # noqa: E731

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
            else 'Local developer preview (linked mode — not redacted, do not share).'
        )
        self._write_page(self.out_dir / 'index.html', 'index.html', {
            'surnames': surnames, 'discoveries': discoveries, 'sources': sources,
            'places': places, 'intro': intro, 'root_prefix': '.',
        })

    # — rendering plumbing —

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
                    'Generated by fha site (linked preview — unredacted; do not publish).'
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
        records that became redacted (idempotent regeneration — TOOLING §12)
        without disturbing anything else a human keeps in the output directory."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for d in (self.persons_dir, self.sources_dir, self.places_dir, self.media_dir):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        for f in ('index.html', 'discoveries.html'):
            target = self.out_dir / f
            if target.exists():
                target.unlink()


# ── Core / CLI ──────────────────────────────────────────────────────────────

def run_site(
    archive_root: Path,
    out_dir: Path,
    *,
    linked: bool = False,
    dry_run: bool = False,
) -> dict:
    """Library entry point. Build the site and return a result dict.

    Returns {'status', 'messages', 'out_dir', 'pages'} where status is one of:
      'no-jinja'    — Jinja2 not installed (CLI prints an install hint)
      'no-index'    — index absent/unreadable (open_index_db already explained)
      'bad-output'  — output dir would clobber archive content (CLI explains)
      'dry-run'     — would build N pages; nothing written
      'ok'          — built; 'messages' non-empty means finished with warnings
    """
    if jinja2 is None:
        return {'status': 'no-jinja', 'messages': [], 'out_dir': out_dir, 'pages': 0}

    unsafe = _unsafe_output_reason(out_dir, archive_root)
    if unsafe:
        return {'status': 'bad-output', 'messages': [unsafe], 'out_dir': out_dir, 'pages': 0}

    conn = open_index_db(archive_root, _REQUIRED_TABLES, strict=False)
    if conn is None:
        return {'status': 'no-index', 'messages': [], 'out_dir': out_dir, 'pages': 0}

    builder = _SiteBuilder(conn, archive_root, load_fha_yaml(archive_root), out_dir, linked=linked)
    try:
        builder.prepare()
        if dry_run:
            return {
                'status': 'dry-run', 'messages': builder.messages, 'out_dir': out_dir,
                'pages': (len(builder.source_pages) + len(builder.person_pages)
                          + len(builder.place_pages) + 2),
            }
        pages = builder.run()
        return {'status': 'ok', 'messages': builder.messages, 'out_dir': out_dir, 'pages': pages}
    finally:
        builder.close()
        conn.close()


def _unsafe_output_reason(out_dir: Path, archive_root: Path) -> str | None:
    """Return a plain refusal message if writing the site to `out_dir` would
    overwrite archive content, else None.

    `fha site` clears the `persons/`, `sources/`, and `media/` subtrees of its
    output directory before regenerating (idempotent rebuild). One of those —
    `sources/` — is also the name of the archive's own record tree. So pointing
    `--out` at the archive root (or any existing archive) would delete real
    records. Refuse before any write rather than risk that. The default
    `.cache/site/` is always safe.
    """
    try:
        out_res = out_dir.resolve()
        root_res = archive_root.resolve()
    except OSError:
        return None
    # The catastrophic case: out_dir IS the archive root, so the site's
    # sources/ subtree (cleared on rebuild) would be the archive's own record
    # tree. The normal default (.cache/site, nested *inside* the archive) is
    # fine — only an exact match is the danger.
    if out_res == root_res:
        return (
            f'Refusing to build the site into the archive root ({archive_root}). '
            'The site clears its own sources/ folder when it rebuilds, which would '
            'delete your records. Pick a separate folder, e.g. `--out .cache/site` (the default).'
        )
    # A different archive (its own fha.yaml + record tree) must not be clobbered
    # either — but the archive's own .cache/ etc. never carries an fha.yaml.
    if out_res != root_res and (out_dir / 'fha.yaml').exists():
        return (
            f'Refusing to build the site into {out_dir}: it looks like another archive '
            '(it has an fha.yaml). Choose an empty or site-only folder, e.g. the default `.cache/site`.'
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
    if status == 'bad-output':
        return EXIT_FAILURE   # the refusal message is already in result['messages']

    mode = 'linked preview' if getattr(args, 'linked', False) else 'standalone snapshot'
    where = _display_path(result['out_dir'], archive_root)
    if status == 'dry-run':
        print(f'(dry run — no files written) Would build {result["pages"]} pages ({mode}) in {where}')
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
