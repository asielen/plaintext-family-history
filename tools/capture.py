#!/usr/bin/env python3
"""
capture.py - fha capture: the web-record intake on-ramp (TOOLING §13b).

  fha capture [--url URL] [--title "…"] [--type TYPE] [--date DATE] [--asset FILE]

Capture turns *an open web record page* into a **source stub** in `inbox/`
(SPEC §12.1) - never a finished Source. It reads the page HTML the human already
has in front of them (piped on stdin, or read from `--asset` when that file is
the saved page) and writes a `{slug}.notes.md` stub whose light frontmatter holds
the citation fields a recipe could recover and whose body is the page's visible
text. A later `fha process` session (the `process-source` skill) mints the S-id,
drafts claims, and promotes the stub into a real record - capture only *stages*.

This is the paste-fallback delivery form: it needs no browser extension, reads
only what is handed to it, and never logs in or fetches behind auth (the §13b
boundary). The browser companion is a thinner front-end onto this same backend.

Two extraction layers:

  * **Site recipes** (`tools/capture_recipes/`, MG1.2/MG1.3) - Ancestry,
    FamilySearch, Newspapers.com, FindAGrave each know where that site keeps the
    title, date, collection, repository, image URL, and the persons it lists.
    Recipes are *data*: a module exposing `detect(html, url)` and
    `extract(html, url)`, discovered at runtime and tried in priority order.
  * **Generic recipe** (this file) - the universal fallback for an unknown
    site: page title, canonical/`--url`, accessed-date, and visible text as the
    citation basis. Any page is capturable, just with more to fix in review.

`--title` / `--type` / `--date` always override whatever a recipe (or the
generic pass) inferred - the human's explicit word wins. Capture also writes a
research-log entry (capture is itself a logged search, closing the §16 loop):
into the live index's `search_log` when an index exists, else appended to
`.cache/capture_log.jsonl`.

A third mode, `fha capture --path PATH`, has nothing to do with web pages: it
registers a file (most often a photo) that must stay exactly where it is - the
photo library is never reorganized. It reads no HTML and stages no asset copy;
it writes one pointer stub (`asset_elsewhere: true` + the path, both as given
and resolved) so the item enters the inbox processing queue without ever being
moved, renamed, or opened.

Stdlib only - the page is parsed with `html.parser`, never a third-party HTML
library (the project adds no dependency before Jinja2 in M8).
"""

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  HTML parsing (stdlib html.parser - shared with the recipes)
#    _PageParser               - one pass: title / first h1 / base / canonical / meta / text
#    parse_html                - _PageParser → a ParsedPage of the fields recipes read
#    _TableParser, extract_tables - table rows as text grids (household/index tables)
#    visible_text, domain_of, meta_content, first_nonempty - recipe-facing helpers
#
#  Recipe layer
#    RecipeResult              - the normalized citation fields a recipe returns
#    generic_extract           - the universal fallback recipe
#    _load_site_recipes        - discover capture_recipes/*.py, sorted by PRIORITY
#    choose_recipe             - first recipe whose detect() matches, else generic
#
#  Stub assembly
#    _slugify / _yaml_inline   - slug + safe single-line YAML scalar
#    _render_stub              - RecipeResult + body → the inbox notes.md text
#    _unique_stub_stem         - collision-free {stem} for the stub (+ its asset)
#
#  Research log
#    _write_capture_log        - search_log row (index present) or capture_log.jsonl
#
#  Top-level + CLI
#    run_capture               - read HTML, choose recipe, write stub + asset + log
#    run_ingest                - sweep staged bundles (§6) → run_capture per bundle
#    _resolve_staging_dir / _iter_bundles / _read_bundle / _read_scrape_source
#    _park_ingested
#    _render_pointer_stub      - the --path mode's stub text (no recipe, no HTML)
#    run_capture_path          - register a must-never-move asset by path
#    register / _run_capture / _standalone_main
#
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime
import functools
import importlib
import json
import os
import pkgutil
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    SOURCE_TYPES,
    FhaConfigError,
    Result,
    configure_utf8_stdout,
    format_edtf_error,
    format_source_type_error,
    is_valid_edtf,
    load_fha_yaml,
    normalize_date,
    read_record,
    resolve_path,
    resolve_root_arg,
)

import yaml

configure_utf8_stdout()

# The generic fallback's source_type. SPEC §14 / _lib.SOURCE_TYPES spell the
# web vocabulary term `website` (BUILD_INGESTION.md MG1.1 writes the shorthand `web`); we
# emit the controlled-vocabulary value so the staged stub processes cleanly -
# `fha process` refuses an out-of-vocabulary source_type hint.
_GENERIC_SOURCE_TYPE = 'website'

# A trailing " | Site Name" / " — Site Name" tail on a page <title> is site
# chrome, not the record title (e.g. KU Libraries Digital Collections). Stripped
# only off the page.title fallback, never off og:title. The plain hyphen is
# deliberately NOT a separator here: a record title like "Jane Smith - 1920
# Census" legitimately uses one, and stripping it would drop the descriptor (and
# hide the year from the harvest below).
_SITE_SUFFIX_RE = re.compile(r'\s+[|–—]\s+[^|–—]+$')

# A four-digit year (1500s-2099) to harvest as the source_date guess when a page
# ships no explicit published date - the same window the recipes' harvest uses.
_GENERIC_YEAR_RE = re.compile(r'\b(1[5-9]\d{2}|20\d{2})\b')


def _strip_site_suffix(title: str | None) -> str | None:
    """Drop a trailing " | / - / — Site Name" tail; keep the title if that's all."""
    if not title:
        return title
    stripped = _SITE_SUFFIX_RE.sub('', title).strip()
    return stripped or title


# Visible-text body is a citation *basis*, not the whole page - cap it so a long
# article doesn't bloat every stub (BUILD_INGESTION.md MG1.1: "visible text … ~2000 chars").
_BODY_CHAR_CAP = 2000

# Staged-bundle `capture.json` schema version (TOOLING_INGESTION §3). Bump only on
# an INCOMPATIBLE shape change. Ingest is forgiving: an absent `schema` is treated
# as current (legacy/hand-authored bundles), and a NEWER schema is read for the
# fields it shares with a one-line warning - never refused. So a companion can add
# fields without breaking older tools, and newer tools read older bundles as-is.
#
# Schema 2 replaced the single `asset_mode`/`asset_file` pair with an
# `assets: [{file, role, mode, provisional?}]` LIST so one capture can carry BOTH
# a self-contained page copy (role `webpage`) AND a separate evidence file (role
# `record`) - the "both" case. Ingest reads BOTH shapes: schema 1's flat pair is
# still honored, so older bundles keep filing unchanged (`_read_bundle`).
#
# How each lands in the inbox follows SPEC §12.1's stub grammar:
#   • zero or one asset → the lone-sidecar stub `{stem}.notes.md` (+ same-stem
#     asset, or pointer-only) - `run_capture`, unchanged.
#   • two or more assets → a §12.1 BUNDLE FOLDER `inbox/<slug>/` holding a single
#     `notes.md` (freeform body + light frontmatter hints, incl. per-file `files:`
#     role hints) plus every asset, exactly the shape `fha process` dissolves into
#     one source with a populated `files:` inventory - `_ingest_bundle_folder`.
_CAPTURE_JSON_SCHEMA = 2

# Role given to a staged asset that carries no explicit role: it is the record
# evidence, the natural primary of the bundle.
_DEFAULT_ASSET_ROLE = 'record'


def _today() -> str:
    return datetime.date.today().isoformat()


# ── HTML parsing ──────────────────────────────────────────────────────────────

@dataclass
class ParsedPage:
    """The handful of fields every recipe reads off a page (one parse pass).

    `meta` maps a lowercased `<meta name=…>`/`<meta property=…>` to its content
    (so `og:title`, `article:published_time`, etc. are one dict lookup). `text`
    is the collapsed visible text with `<script>`/`<style>`/`<title>` removed.
    """
    title: str | None
    h1: str | None
    base_href: str | None
    canonical: str | None
    meta: dict
    text: str


class _PageParser(HTMLParser):
    """Collect title / first h1 / base href / canonical / meta / visible text.

    A single forgiving pass (HTMLParser tolerates the malformed markup real
    pages ship). `<script>`/`<style>` bodies are dropped from the visible text;
    `<title>` text is captured for the title but kept out of the body so the
    snippet reads as page content, not chrome.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._title_parts: list[str] = []
        self._in_title = False
        self._h1_parts: list[str] = []
        self._in_h1 = False
        self._h1_done = False
        self.base_href: str | None = None
        self.canonical: str | None = None
        self.meta: dict[str, str] = {}
        self._text_parts: list[str] = []
        self._skip_depth = 0  # inside <script>/<style>

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = {k.lower(): (v or '') for k, v in attrs}
        if tag in ('script', 'style'):
            self._skip_depth += 1
        elif tag == 'title':
            self._in_title = True
        elif tag == 'h1' and not self._h1_done:
            self._in_h1 = True
        elif tag == 'base' and a.get('href'):
            self.base_href = self.base_href or a['href']
        elif tag == 'link' and 'canonical' in a.get('rel', '').lower() and a.get('href'):
            self.canonical = self.canonical or a['href']
        elif tag == 'meta':
            key = a.get('property') or a.get('name')
            if key and 'content' in a:
                self.meta.setdefault(key.lower(), a['content'])

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        # Self-closing <meta .../> / <base .../> / <link .../> never fire an end
        # tag, so route them through the same start handler.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in ('script', 'style') and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == 'title':
            self._in_title = False
        elif tag == 'h1' and self._in_h1:
            self._in_h1 = False
            self._h1_done = True

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._in_h1:
            self._h1_parts.append(data)
        self._text_parts.append(data)

    def title(self) -> str | None:
        return _clean_ws(''.join(self._title_parts)) or None

    def h1(self) -> str | None:
        return _clean_ws(''.join(self._h1_parts)) or None

    def text(self) -> str:
        return _clean_ws(' '.join(self._text_parts))


def _clean_ws(text: str) -> str:
    """Collapse all runs of whitespace to single spaces and trim."""
    return ' '.join((text or '').split())


def parse_html(html: str) -> ParsedPage:
    """Parse page HTML into the fields recipes and the generic pass read."""
    parser = _PageParser()
    try:
        parser.feed(html or '')
    except Exception:
        # HTMLParser is forgiving, but a pathological document can still raise;
        # whatever was collected before the break is still usable.
        pass
    return ParsedPage(
        title=parser.title(),
        h1=parser.h1(),
        base_href=parser.base_href,
        canonical=parser.canonical,
        meta=parser.meta,
        text=parser.text(),
    )


class _TableParser(HTMLParser):
    """Collect every `<table>` as a grid of cell texts (rows × cells).

    Recipes that read a census household table or a record index table get a
    plain `list[list[list[str]]]` rather than each re-implementing tag tracking.
    Header (`<th>`) and data (`<td>`) cells are both kept, in document order.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == 'table':
            self._depth += 1
            self.tables.append([])
        elif tag == 'tr' and self._depth:
            self._row = []
        elif tag in ('td', 'th') and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == 'table' and self._depth:
            self._depth -= 1
        elif tag == 'tr' and self._row is not None:
            self.tables[-1].append(self._row)
            self._row = None
        elif tag in ('td', 'th') and self._cell is not None:
            self._row.append(_clean_ws(''.join(self._cell)))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def extract_tables(html: str) -> list[list[list[str]]]:
    """Return every `<table>` in `html` as a grid of cell-text rows."""
    parser = _TableParser()
    try:
        parser.feed(html or '')
    except Exception:
        pass
    return parser.tables


def domain_of(url: str | None) -> str:
    """Return the bare host of a URL (`www.` stripped), or '' when unparseable."""
    if not url:
        return ''
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith('www.') else host


def meta_content(page: ParsedPage, *keys: str) -> str | None:
    """First non-empty `<meta>` content among `keys` (already lowercased)."""
    for key in keys:
        val = page.meta.get(key.lower())
        if val and val.strip():
            return val.strip()
    return None


def first_nonempty(*values: str | None) -> str | None:
    """First value that is a non-blank string, else None."""
    for v in values:
        if v and str(v).strip():
            return str(v).strip()
    return None


def visible_text(html_or_page: str | ParsedPage, *, cap: int = _BODY_CHAR_CAP) -> str:
    """Collapsed visible text, truncated to `cap` chars at a word boundary."""
    text = html_or_page.text if isinstance(html_or_page, ParsedPage) else parse_html(html_or_page).text
    if len(text) <= cap:
        return text
    cut = text[:cap]
    # Don't slice mid-word: back up to the last space when one is near the cap.
    space = cut.rfind(' ')
    if space > cap - 40:
        cut = cut[:space]
    return cut.rstrip() + ' …'


# ── Recipe layer ──────────────────────────────────────────────────────────────

@dataclass
class RecipeResult:
    """Normalized citation fields a recipe (or the generic pass) produces.

    A recipe may fill any subset; `run_capture` supplies defaults, stamps the
    accessed-date on every `external_links` entry, and lets the explicit
    `--title`/`--type`/`--date` flags override. `collection`/`terms` are not
    written to the stub - they feed the research-log entry (§16).
    """
    title: str | None = None
    source_type: str = _GENERIC_SOURCE_TYPE
    citation: str | None = None
    repository: str | None = None
    source_date: str | None = None
    external_links: list[dict] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    body: str = ''
    collection: str = ''
    terms: str = ''


def generic_extract(html: str, url: str | None) -> RecipeResult:
    """The universal fallback: title, URL, accessed-date, visible text.

    Used for any page no site recipe claims. Everything it produces is a
    citation *basis* the reviewer refines - the source_type is the generic
    `website` and the citation is assembled from whatever the page exposed.
    """
    page = parse_html(html)
    page_url = first_nonempty(url, page.canonical, page.base_href,
                              meta_content(page, 'og:url'))
    # Prefer the clean og:title; fall back to page.title with its site-suffix
    # stripped (a print-shop run-on or " | Site" tail is not the record title),
    # then the h1.
    title = first_nonempty(
        meta_content(page, 'og:title'),
        _strip_site_suffix(page.title),
        page.h1,
    ) or (domain_of(page_url) or 'captured page')
    repository = domain_of(page_url)
    accessed = _today()

    # An explicit published date wins; otherwise harvest a year from the title
    # or og:description (a citation guess the reviewer refines).
    source_date = first_nonempty(meta_content(page, 'article:published_time'))
    if not source_date:
        ym = _GENERIC_YEAR_RE.search(
            ' '.join(filter(None, [title, meta_content(page, 'og:description')])))
        source_date = ym.group(1) if ym else None

    citation_bits = [title.rstrip('.')]
    if repository:
        citation_bits.append(repository)
    if page_url:
        citation_bits.append(page_url)
    citation = '. '.join(citation_bits) + f' (accessed {accessed}).'

    external_links = [{'url': page_url}] if page_url else []
    return RecipeResult(
        title=title,
        source_type=_GENERIC_SOURCE_TYPE,
        citation=citation,
        repository=repository or None,
        source_date=source_date,
        external_links=external_links,
        people=[],
        body=visible_text(page),
        collection='',
        terms='',
    )


@functools.lru_cache(maxsize=1)
def _load_site_recipes() -> list:
    """Import every `tools/capture_recipes/*.py` recipe module, sorted by PRIORITY.

    Recipes are plug-in *data* for capture (the §13b "recipe set is data"
    promise), not sibling tools, so importing them here does not breach the
    tools-never-import-tools rule. A module missing the recipe interface, or one
    that fails to import, is skipped with a warning rather than taking the whole
    command down - an unknown page still captures via the generic fallback.
    Discovery is cached per process because batch/test runs may call
    `run_capture` many times while the recipe set itself is static.
    """
    pkg_dir = Path(__file__).parent / 'capture_recipes'
    if not pkg_dir.is_dir():
        return []
    recipes = []
    for info in pkgutil.iter_modules([str(pkg_dir)]):
        if info.name.startswith('_'):
            continue
        try:
            mod = importlib.import_module(f'capture_recipes.{info.name}')
        except Exception as e:  # noqa: BLE001 - a broken recipe must not abort capture
            print(f'WARNING: skipping capture recipe {info.name!r}: {e}', file=sys.stderr)
            continue
        if not (hasattr(mod, 'detect') and hasattr(mod, 'extract')):
            print(f'WARNING: capture recipe {info.name!r} lacks detect/extract; skipping',
                  file=sys.stderr)
            continue
        recipes.append(mod)
    recipes.sort(key=lambda m: getattr(m, 'PRIORITY', 1000))
    return recipes


def choose_recipe(html: str, url: str | None, recipes: list) -> tuple[str, RecipeResult]:
    """Run the first recipe whose `detect` matches; else the generic fallback.

    Returns `(source_name, result)`. A recipe whose `detect`/`extract` raises is
    treated as a non-match (warned, then skipped) so one site's quirk never
    blocks capturing the page generically.
    """
    for mod in recipes:
        name = getattr(mod, 'SOURCE_NAME', mod.__name__.rsplit('.', 1)[-1])
        try:
            if not mod.detect(html, url):
                continue
            raw = mod.extract(html, url)
        except Exception as e:  # noqa: BLE001
            print(f'WARNING: capture recipe {name!r} failed ({e}); trying the next',
                  file=sys.stderr)
            continue
        return name, _coerce_result(raw)
    return 'generic', generic_extract(html, url)


def _coerce_result(raw) -> RecipeResult:
    """Accept a recipe's dict (or RecipeResult) and normalize to a RecipeResult."""
    if isinstance(raw, RecipeResult):
        return raw
    if not isinstance(raw, dict):
        raise TypeError(f'recipe extract() must return a dict or RecipeResult, got {type(raw).__name__}')
    return RecipeResult(
        title=raw.get('title'),
        source_type=raw.get('source_type') or _GENERIC_SOURCE_TYPE,
        citation=raw.get('citation'),
        repository=raw.get('repository'),
        source_date=raw.get('source_date'),
        external_links=list(raw.get('external_links') or []),
        people=list(raw.get('people') or []),
        body=raw.get('body') or '',
        collection=raw.get('collection') or '',
        terms=raw.get('terms') or '',
    )


# ── Stub assembly ─────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    """Collapse text to a lowercase-hyphenated slug (matches process.py / SPEC §13)."""
    text = (text or '').strip().lower()
    slug = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return slug or 'capture'


def _yaml_inline(value: str) -> str:
    """Render a string as a single-line YAML scalar, quoting only when needed.

    The same discipline `process.py` uses: free-form citation/title/URL text can
    carry YAML-significant characters (`: `, a leading `-`), so each scalar is
    round-tripped through the emitter and quoted exactly when the parser needs
    it - otherwise the stub's frontmatter would not re-parse on `fha process`.
    """
    rendered = yaml.safe_dump(
        value, default_flow_style=True, allow_unicode=True, width=10 ** 9,
    ).strip()
    if rendered.endswith('...'):
        rendered = rendered[:-3].strip()
    return rendered


def _render_stub(result: RecipeResult, *, accessed: str, has_asset: bool) -> str:
    """Render the inbox `*.notes.md` source stub (SPEC §12.1) as text.

    Light, optional frontmatter (the recipe's citation fields) over a freeform
    body - never a §14 record. `people:` lists the *names* the page showed, a
    hint the processing pass reconciles against the index; it is not the §14
    P-id `people:` list (a stub has no resolved P-ids yet).

    `has_asset` is False for TOOLING §13b case (c) - the page only points
    elsewhere, no downloadable image/document and no HTML snapshot. That
    pointer-only case is flagged `asset_elsewhere: true` so `fha process`
    knows the missing companion is deliberate, not an oversight (the explicit
    flag `process.py`'s `_companion_for_sidecar` requires before it will mint
    a no-asset source record).
    """
    lines = ['---']
    if result.title:
        lines.append(f'title: {_yaml_inline(result.title)}')
    lines.append(f'source_type: {result.source_type}')
    if not has_asset and result.external_links:
        lines.append('asset_elsewhere: true')
    if result.citation:
        lines.append('citation: >')
        lines += [f'  {ln}' for ln in (result.citation.splitlines() or [''])]
    if result.repository:
        lines.append(f'repository: {_yaml_inline(result.repository)}')
    if result.source_date:
        lines.append(f'source_date: {_yaml_inline(result.source_date)}')
    if result.external_links:
        lines.append('external_links:')
        for link in result.external_links:
            url = link.get('url') if isinstance(link, dict) else str(link)
            if not url:
                continue
            lines.append(f'  - url: {_yaml_inline(str(url))}')
            link_accessed = (link.get('accessed') if isinstance(link, dict) else None) or accessed
            lines.append(f'    accessed: {_yaml_inline(str(link_accessed))}')
    if result.people:
        lines.append('people:')
        lines += [f'  - {_yaml_inline(str(name))}' for name in result.people]
    lines.append('---')
    lines.append('')
    body = result.body.strip()
    lines.append(body if body else '*(captured page - no visible text extracted)*')
    lines.append('')
    return '\n'.join(lines)


def _unique_stub_stem(inbox: Path, slug: str, asset_suffix: str | None = None) -> str:
    """Return a `{stem}` (slug, else slug-2, slug-3 …) free of a `.notes.md` clash.

    The stub and its optional asset share this stem so they pair by basename
    (SPEC §12.1 lone-sidecar rule) - so the collision check looks at the stub
    name and the optional asset name. `shutil.copy2` overwrites by default, so
    the asset collision check is part of the safety contract, not decoration.
    """
    stem = slug
    n = 2
    suffix = (asset_suffix or '').lower()
    while (
        (inbox / f'{stem}.notes.md').exists()
        or (suffix and (inbox / f'{stem}{suffix}').exists())
    ):
        stem = f'{slug}-{n}'
        n += 1
    return stem


# ── Research log ──────────────────────────────────────────────────────────────

_SEARCH_LOG_DDL = '''CREATE TABLE IF NOT EXISTS search_log(
  date TEXT, person_id TEXT, question TEXT, repository TEXT, collection TEXT,
  terms TEXT, result TEXT, source_id TEXT, path TEXT
)'''


def _write_capture_log(
    archive_root: Path,
    *,
    date: str,
    question: str,
    repository: str,
    collection: str,
    terms: str,
    result: str,
    stub_rel: str,
) -> str:
    """Record the capture as a research-log search (§16): index row + jsonl.

    Capture is itself a logged search, so `fha report`'s "already searched"
    annotation can see it the moment it lands. The row always appends to
    `.cache/capture_log.jsonl` first - that file is the durable record `fha
    index` re-ingests into `search_log` on every full rebuild (which drops and
    recreates that table from scratch), so a capture survives a reindex even
    though the table itself doesn't persist it. When `.cache/index.sqlite`
    already exists, the row is *also* written straight into its `search_log`
    table (`person_id`/`source_id` are null - a stub has no resolved person
    and no S-id yet) so `fha report` sees it immediately, without waiting for
    the next rebuild. Returns 'index' or 'jsonl' for the caller's status line
    (favoring 'index' when both succeed); a logging failure is reported but
    never fails the capture (the stub is already safely written).
    """
    cache_dir = archive_root / '.cache'
    db_path = cache_dir / 'index.sqlite'
    row = (date, None, question, repository, collection, terms, result, None, stub_rel)

    jsonl_ok = False
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_dir / 'capture_log.jsonl', 'a', encoding='utf-8') as f:
            f.write(json.dumps({
                'date': date, 'question': question, 'repository': repository,
                'collection': collection, 'terms': terms, 'result': result,
                'path': stub_rel,
            }, ensure_ascii=False) + '\n')
        jsonl_ok = True
    except OSError as e:
        print(f'WARNING: could not write capture_log.jsonl: {e}', file=sys.stderr)

    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(_SEARCH_LOG_DDL)
                conn.execute(
                    '''INSERT INTO search_log
                       (date, person_id, question, repository, collection, terms,
                        result, source_id, path)
                       VALUES (?,?,?,?,?,?,?,?,?)''',
                    row,
                )
                conn.commit()
            finally:
                conn.close()
            return 'index'
        except Exception as e:  # noqa: BLE001
            print(f'WARNING: could not write search_log row ({e})'
                  + (' (still logged to jsonl)' if jsonl_ok else ''), file=sys.stderr)

    return 'jsonl' if jsonl_ok else 'none'


# ── Top-level ─────────────────────────────────────────────────────────────────

class CaptureError(Exception):
    """A user-facing capture failure (bad input or refusal)."""


class CaptureWriteError(Exception):
    """A filesystem failure while staging a capture.

    Bad flags and unsafe inputs are user errors (exit 2). A failed mkdir,
    write, or asset copy means the tool could not complete a requested
    mutation, so the CLI must report the standard tool-failure exit code
    instead of looking like the page itself was invalid.
    """


class BundleError(Exception):
    """A staged bundle that `--ingest` cannot read (TOOLING_INGESTION §6).

    A malformed bundle (missing `page.html`, unreadable/invalid `capture.json`)
    is reported and left in place - never half-ingested, never silently dropped
    - and must not abort the sweep of its sibling bundles.
    """


def _read_html(asset: Path | None) -> str:
    """Read page HTML from stdin, falling back to an HTML `--asset` file.

    The paste-fallback path pipes the page on stdin (`… | fha capture`); when no
    stdin is piped, an `--asset` that is itself the saved page is read as the
    HTML (BUILD_INGESTION.md MG1.1: "Read HTML from stdin or `--asset`"). A binary asset
    (an image download) yields no usable HTML - the generic recipe then works
    from `--url`/`--title` alone.
    """
    data = ''
    if not sys.stdin.isatty():
        try:
            # Read the raw bytes and decode UTF-8 explicitly: sys.stdin.read()
            # would use the locale encoding (cp1252 on Windows), turning a piped
            # UTF-8 page's en-dashes and accents into mojibake that then breaks
            # date/name extraction. errors='replace' keeps a stray byte from
            # aborting the whole capture.
            raw = sys.stdin.buffer.read()
            data = raw.decode('utf-8', errors='replace')
        except Exception:
            data = ''
    if data.strip():
        return data
    if asset is not None and asset.suffix.lower() in ('.html', '.htm', '.xhtml'):
        try:
            return asset.read_text(encoding='utf-8', errors='replace')
        except OSError as e:
            raise CaptureError(f'could not read --asset {asset.name}: {e}')
    return ''


def _resolve_recipe_result(
    *,
    url: str | None,
    title: str | None,
    source_type: str | None,
    source_date: str | None,
    html: str,
    notes: str | None,
    people: list[str] | None,
    repository: str | None = None,
) -> tuple[str, RecipeResult]:
    """Choose a recipe and apply the explicit/override fields (the human's nudge).

    Shared by the lone-sidecar path (`run_capture`) and the §12.1 bundle-folder
    path (`_ingest_bundle_folder`), so both resolve title/type/date/notes/people
    identically. `--title`/`--type`/`--date` (and the staged-bundle
    `notes`/`people` overrides) always win over the recipe's scrape (§13b).
    Returns `(recipe_name, result)`.
    """
    recipe_name, result = choose_recipe(html, url, _load_site_recipes())

    if title:
        result.title = title
    if repository:
        # A human correction to "Where it's from" wins over the recipe/host
        # guess, like --title/--type/--date.
        result.repository = repository
    if source_type:
        st = source_type.strip().lower()
        if st not in SOURCE_TYPES:
            raise CaptureError(format_source_type_error(source_type, where='--type'))
        result.source_type = st
    elif result.source_type not in SOURCE_TYPES:
        # A recipe must still stay within vocabulary; the generic default already
        # does, but a misbehaving recipe shouldn't write a bad stub.
        result.source_type = _GENERIC_SOURCE_TYPE
    if source_date:
        normalized = normalize_date(source_date)
        if normalized is None:
            raise CaptureError(format_edtf_error(source_date, field='--date'))
        result.source_date = normalized
    elif result.source_date and not is_valid_edtf(str(result.source_date)):
        normalized = normalize_date(str(result.source_date))
        if normalized is None:
            print(f'WARNING: recipe produced a source_date {result.source_date!r} '
                  'that the archive could not read; dropping it from the stub.',
                  file=sys.stderr)
            result.source_date = None
        else:
            result.source_date = normalized

    # Staged-bundle overrides (TOOLING_INGESTION §6): the human's curated body and
    # names win over the recipe's scrape, exactly like --title/--type/--date above.
    if notes is not None:
        result.body = notes
    if people is not None:
        # The human's curated names come first, then any recipe-found name not
        # already present (case-insensitive dedup, preserving each name's
        # first-seen spelling). This is additive on purpose: the panel's people
        # come from the in-browser JSON-LD/microdata harvest, while the Python
        # recipe extracts household/family people from tables the panel never
        # showed - replacing rather than merging would silently DROP those
        # recipe-found relatives (e.g. a Find a Grave family list). People are
        # review hints the reviewer reconciles, so extra noise is tolerable but
        # lost relatives are not. (Place-shaped label noise is already kept out
        # by the _common.py people guard.)
        seen = {name.strip().lower() for name in people}
        merged = list(people)
        merged += [n for n in result.people if n.strip().lower() not in seen]
        result.people = merged

    # An explicit url that no recipe surfaced still belongs in external_links.
    if url and not any((isinstance(l, dict) and l.get('url') == url) for l in result.external_links):
        result.external_links.insert(0, {'url': url})

    return recipe_name, result


def run_capture(
    archive_root: Path,
    fha_config: dict,
    *,
    url: str | None,
    title: str | None,
    source_type: str | None,
    source_date: str | None,
    asset: Path | None,
    html: str,
    accessed: str | None = None,
    notes: str | None = None,
    people: list[str] | None = None,
    repository: str | None = None,
    dry_run: bool = False,
) -> Result:
    """Capture a page into an inbox source stub and log the search (TOOLING §13b).

    Returns a `Result` (Result == int, so callers/tests comparing against EXIT_*
    keep working).  The capture narration is printed inline as the stub/asset are
    staged - those side effects and their progress lines stay here per the
    structured-result contract - and the staged stub/asset are listed in
    `changed` (empty under --dry-run).  Raises CaptureError/CaptureWriteError for
    the `_run_capture` bridge to translate into exit codes.

    `accessed`/`notes`/`people` are the staged-bundle override seam (the
    `--ingest` sweep, TOOLING_INGESTION §6): they default to inert (`None`), so
    the paste-fallback path is byte-identical.  When supplied they win over the
    scrape the same way `--title`/`--type`/`--date` do - `accessed` is the date
    the human actually viewed the page, `notes` is their free-text body, and
    `people` are their curated name hints (unioned ahead of any recipe-found
    names, deduplicated).
    """
    recipe_name, result = _resolve_recipe_result(
        url=url, title=title, source_type=source_type, source_date=source_date,
        html=html, notes=notes, people=people, repository=repository,
    )

    accessed = accessed or _today()
    if not result.title:
        result.title = domain_of(url) or 'captured page'

    slug = _slugify(result.title)
    inbox = resolve_path('inbox', fha_config, archive_root)
    asset_suffix = asset.suffix.lower() if asset is not None else None
    stem = _unique_stub_stem(inbox, slug, asset_suffix)
    stub_path = inbox / f'{stem}.notes.md'
    stub_text = _render_stub(result, accessed=accessed, has_asset=asset is not None)

    asset_dest: Path | None = None
    if asset is not None:
        asset_dest = inbox / f'{stem}{asset_suffix}'

    matched = 'generic recipe' if recipe_name == 'generic' else f'{recipe_name} recipe'
    if dry_run:
        print(f'[dry-run] Would capture via {matched} ({result.source_type})')
        print(f'[dry-run] Would stage stub {_rel(stub_path, archive_root)}')
        if asset_dest is not None:
            print(f'[dry-run] Would copy asset {_rel(asset_dest, archive_root)}')
        print('[dry-run] Would log the search in the index or .cache/capture_log.jsonl')
        return Result(exit_code=EXIT_CLEAN, data={'status': 'dry-run', 'recipe': recipe_name})

    try:
        inbox.mkdir(parents=True, exist_ok=True)
        stub_path.write_text(stub_text, encoding='utf-8')
    except OSError as e:
        raise CaptureWriteError(
            f'could not stage stub {_rel(stub_path, archive_root)}: {e}'
        ) from e
    if asset is not None and asset_dest is not None:
        if asset_dest.exists():
            stub_path.unlink(missing_ok=True)
            raise CaptureError(
                f'inbox asset destination already exists: {_rel(asset_dest, archive_root)}'
            )
        try:
            shutil.copy2(asset, asset_dest)
        except OSError as e:
            stub_path.unlink(missing_ok=True)
            raise CaptureWriteError(
                f'could not copy --asset {asset.name} into the inbox: {e}'
            ) from e

    stub_rel = _rel(stub_path, archive_root)
    log_sink = _write_capture_log(
        archive_root,
        date=accessed,
        question=result.title or '',
        repository=result.repository or domain_of(url),
        collection=result.collection,
        terms=result.terms,
        result=f'staged {stub_rel}',
        stub_rel=stub_rel,
    )

    print(f'Captured via {matched} ({result.source_type})')
    print(f'Staged stub {stub_rel}')
    if asset_dest is not None:
        print(f'Copied asset {_rel(asset_dest, archive_root)}')
    if result.people:
        print(f'Person hints: {", ".join(result.people)}')
    if log_sink == 'index':
        print('Logged the search in the index (search_log)')
    elif log_sink == 'jsonl':
        print('Logged the search in .cache/capture_log.jsonl')
    changed = [str(stub_path)]
    if asset_dest is not None:
        changed.append(str(asset_dest))
    return Result(
        exit_code=EXIT_CLEAN,
        data={'status': 'ok', 'recipe': recipe_name, 'stub': stub_rel},
        changed=changed,
    )


def _rel(path: Path, archive_root: Path) -> str:
    """Display a path relative to the archive root when possible, else posix."""
    try:
        return path.resolve().relative_to(archive_root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


# ── --path: register an asset that must never move ──────────────────────────

def _render_pointer_stub(
    *, title: str | None, note: str | None, given_path: str, absolute_path: str,
) -> str:
    """Render the `--path` pointer stub: no recipe, no HTML, just a location.

    `asset_elsewhere: true` reuses the existing TOOLING §13b case-(c) flag - a
    stub with no same-stem companion asset sitting beside it in the inbox -
    so a later `fha process` does not treat the missing local file as a
    mistake (`_companion_for_sidecar` in process.py already treats that flag
    as deliberate). `asset_path` / `asset_path_absolute` are new fields: no
    existing stub key names an elsewhere-asset's actual location (the case-c
    flag only ever says THAT one is missing, never WHERE it lives), so this
    build adds them rather than overload a boolean. Both carry forward
    slashes for a stable cross-platform read; `asset_path` is the path
    exactly as the human typed it (their own shorthand may be meaningful to
    them - a mapped drive letter, a relative note-to-self), `asset_path_absolute`
    is the resolved, unambiguous form a future `fha process` pass can act on
    regardless of the working directory it runs from.
    """
    lines = ['---']
    if title:
        lines.append(f'title: {_yaml_inline(title)}')
    lines.append('asset_elsewhere: true')
    lines.append(f'asset_path: {_yaml_inline(given_path)}')
    lines.append(f'asset_path_absolute: {_yaml_inline(absolute_path)}')
    lines.append('---')
    lines.append('')
    body = (note or '').strip()
    lines.append(body if body else '*(registered by path - no note given)*')
    lines.append('')
    return '\n'.join(lines)


def run_capture_path(
    archive_root: Path,
    fha_config: dict,
    *,
    path: str,
    note: str | None = None,
    title: str | None = None,
    dry_run: bool = False,
    check_path: str | Path | None = None,
) -> Result:
    """Register a must-never-move asset: write ONE pointer stub, touch nothing else.

    `check_path` is NOT a CLI concern - `fha capture --path` always resolves
    `path` itself against its own process cwd, the natural reading for a
    human-typed shell argument. It exists for a caller (`fha serve`'s
    capture.path verb) whose relative paths mean something else: the server
    process has no cwd meaningful to the browser, so the workbench resolves
    a relative form value against `archive_root` instead - but that resolved
    form must never overwrite what gets stored as `asset_path` (P2 codex
    finding, round 6, PR #30: a typed `photos/grandma.jpg` was being stored
    as a machine-specific absolute path). Passing the archive-relative
    candidate here lets it drive the existence check / `asset_path_absolute`
    while `path` itself - stored verbatim as `asset_path` below - stays
    exactly what the human typed.

    Some assets (a photo still living in a family member's own library, a
    document in someone else's archive folder) can never be copied, moved, or
    renamed - but the human still wants it on the processing queue. This mode
    reads no HTML and stages no asset copy; the pointed-at file is only ever
    `.exists()`-checked, never opened, moved, or renamed. Durable identity
    (an embedded `SOURCE:` keyword, per TOOLING §13b's photos-are-never-
    renamed rule) is a later `fha process` pass's job - this tool only gets
    the item into the queue.

    `slug` comes from the target FILE's own stem (there is no page title to
    borrow), so the stub is named after what it points at, not a generic
    placeholder; `_unique_stub_stem` still guards against a same-named stub
    already sitting in the inbox.

    A missing target (an unplugged external drive, a typo'd path) is not a
    hard refusal - the human may be capturing before reconnecting the drive -
    so the stub is still written and the warning is recorded either way; only
    the LIVE write's exit code reflects it (1, warnings), matching the
    module's other dry-run branches, which always report a clean preview
    (exit 0) regardless of what a real run would warn about.

    House engine contract: this returns a Result and never prints - every line
    a terminal run shows lives in `Result.messages` instead, in the same order
    and wording, for `_run_capture` (the CLI layer) to render. That is what
    lets `fha serve` read this function's output as structured messages
    instead of scraping captured stdout/stderr.
    """
    target = Path(check_path) if check_path is not None else Path(path)
    given_path = str(path).replace('\\', '/')
    absolute_path = str(target.resolve()).replace('\\', '/')
    exists = target.exists()

    slug = _slugify(target.stem)
    inbox = resolve_path('inbox', fha_config, archive_root)
    stem = _unique_stub_stem(inbox, slug)
    stub_path = inbox / f'{stem}.notes.md'
    stub_text = _render_pointer_stub(
        title=title, note=note, given_path=given_path, absolute_path=absolute_path)

    missing_warning = (
        f'WARNING: {given_path} not found right now - recorded anyway; '
        'the drive may be unplugged.'
    )

    if dry_run:
        result = Result(exit_code=EXIT_CLEAN, data={'status': 'dry-run', 'exists': exists})
        result.add('info', f'[dry-run] Would register {given_path}')
        if not exists:
            result.add('warning', missing_warning)
        result.add('info', f'[dry-run] Would stage stub {_rel(stub_path, archive_root)}')
        result.add('info', '[dry-run] --- stub contents ---')
        for line in stub_text.splitlines():
            result.add('info', f'[dry-run] {line}')
        result.add('info', '[dry-run] --- end stub contents ---')
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        return result

    result = Result(
        exit_code=(EXIT_CLEAN if exists else EXIT_WARNINGS),
        data={'status': 'ok', 'exists': exists, 'stub': _rel(stub_path, archive_root)},
        changed=[str(stub_path)],
    )
    if not exists:
        result.add('warning', missing_warning)

    try:
        inbox.mkdir(parents=True, exist_ok=True)
        stub_path.write_text(stub_text, encoding='utf-8')
    except OSError as e:
        raise CaptureWriteError(
            f'could not stage stub {_rel(stub_path, archive_root)}: {e}'
        ) from e

    result.add('info', f'Registered {given_path}')
    result.add('info', f'Staged stub {_rel(stub_path, archive_root)}')

    return result


# ── Multi-asset ingest: the §12.1 inbox bundle folder (the "both" case) ─────────

def _unique_bundle_dir(inbox: Path, slug: str) -> Path:
    """Return an inbox bundle-folder path (`slug`, else `slug-2`, …) free of clash."""
    name = slug
    n = 2
    while (inbox / name).exists():
        name = f'{slug}-{n}'
        n += 1
    return inbox / name


def _render_bundle_notes(
    result: RecipeResult,
    *,
    accessed: str,
    assets: list[tuple[str, str]],
) -> str:
    """Render a §12.1 bundle `notes.md`: light hint frontmatter + per-file roles + prose.

    `assets` is a list of `(filename, role)` pairs filed in the bundle folder. The
    frontmatter mirrors the lone-sidecar stub's hints (title/source_type/citation/
    repository/source_date/external_links/people) and adds the `files:` role
    inventory `fha process` reads when it dissolves the bundle into one source
    (each file → the record's `files:` inventory with its role). The body is the
    human's notes (or the page's visible text), which flow into `## Notes`.
    """
    lines = ['---']
    if result.title:
        lines.append(f'title: {_yaml_inline(result.title)}')
    lines.append(f'source_type: {result.source_type}')
    if result.citation:
        lines.append('citation: >')
        lines += [f'  {ln}' for ln in (result.citation.splitlines() or [''])]
    if result.repository:
        lines.append(f'repository: {_yaml_inline(result.repository)}')
    if result.source_date:
        lines.append(f'source_date: {_yaml_inline(result.source_date)}')
    if result.external_links:
        lines.append('external_links:')
        for link in result.external_links:
            url = link.get('url') if isinstance(link, dict) else str(link)
            if not url:
                continue
            lines.append(f'  - url: {_yaml_inline(str(url))}')
            link_accessed = (link.get('accessed') if isinstance(link, dict) else None) or accessed
            lines.append(f'    accessed: {_yaml_inline(str(link_accessed))}')
    if result.people:
        lines.append('people:')
        lines += [f'  - {_yaml_inline(str(name))}' for name in result.people]
    if assets:
        lines.append('files:')
        for filename, role in assets:
            lines.append(f'  - file: {_yaml_inline(filename)}')
            if role:
                lines.append(f'    role: {_yaml_inline(role)}')
    lines.append('---')
    lines.append('')
    body = result.body.strip()
    lines.append(body if body else '*(captured page - no visible text extracted)*')
    lines.append('')
    return '\n'.join(lines)


def _ingest_bundle_folder(
    archive_root: Path,
    fha_config: dict,
    *,
    url: str | None,
    title: str | None,
    source_type: str | None,
    source_date: str | None,
    html: str,
    accessed: str | None,
    notes: str | None,
    people: list[str] | None,
    repository: str | None,
    assets: list[tuple[Path, str]],
    dry_run: bool = False,
) -> Result:
    """File a multi-asset capture as a §12.1 inbox BUNDLE FOLDER (the "both" case).

    SPEC §12.1: the moment a stub has more than one file it is a bundle folder
    `inbox/<slug>/` holding one `notes.md` plus every asset, the folder grouping
    them until `fha process` dissolves it into a single source (one S-id, each
    file in the record's `files:` inventory with its role, notes → `## Notes`).

    The raw `page.html` is the scrape source the recipe re-extracts from here (as
    in the lone-sidecar path); it is NOT copied in as a durable asset, so the
    bundle folder holds only `notes.md` + the page copy + the evidence file (the
    capture's three durable artifacts). Mirrors `run_capture`'s recipe/override
    resolution and research-log write so the only difference is the on-disk shape.
    """
    recipe_name, result = _resolve_recipe_result(
        url=url, title=title, source_type=source_type, source_date=source_date,
        html=html, notes=notes, people=people, repository=repository,
    )
    accessed = accessed or _today()
    if not result.title:
        result.title = domain_of(url) or 'captured page'

    slug = _slugify(result.title)
    inbox = resolve_path('inbox', fha_config, archive_root)
    folder = _unique_bundle_dir(inbox, slug)

    # Plan the destination filenames (keep each asset's own name; the page copy and
    # evidence already arrive uniquely named). A name clash between two assets is a
    # malformed bundle the engine should refuse before writing anything.
    planned: list[tuple[Path, str, str]] = []  # (src, dest_name, role)
    used: set[str] = set()
    for src, role in assets:
        dest_name = src.name
        if dest_name in used:
            raise CaptureError(
                f'bundle has two assets named {dest_name!r}; cannot file them together'
            )
        used.add(dest_name)
        planned.append((src, dest_name, role))

    asset_hints = [(dest_name, role) for _, dest_name, role in planned]
    notes_text = _render_bundle_notes(result, accessed=accessed, assets=asset_hints)

    matched = 'generic recipe' if recipe_name == 'generic' else f'{recipe_name} recipe'
    notes_rel = _rel(folder / 'notes.md', archive_root)
    if dry_run:
        print(f'[dry-run] Would capture via {matched} ({result.source_type})')
        print(f'[dry-run] Would stage bundle folder {_rel(folder, archive_root)}/ with:')
        print(f'[dry-run]   notes.md')
        for _, dest_name, role in planned:
            print(f'[dry-run]   {dest_name}  (role: {role})')
        print('[dry-run] Would log the search in the index or .cache/capture_log.jsonl')
        return Result(exit_code=EXIT_CLEAN, data={'status': 'dry-run', 'recipe': recipe_name})

    try:
        folder.mkdir(parents=True, exist_ok=False)
    except OSError as e:
        raise CaptureWriteError(
            f'could not create bundle folder {_rel(folder, archive_root)}: {e}'
        ) from e
    try:
        (folder / 'notes.md').write_text(notes_text, encoding='utf-8')
        for src, dest_name, _role in planned:
            shutil.copy2(src, folder / dest_name)
    except OSError as e:
        # Roll the partial folder back so a failed write never leaves a half bundle.
        shutil.rmtree(folder, ignore_errors=True)
        raise CaptureWriteError(
            f'could not stage bundle folder {_rel(folder, archive_root)}: {e}'
        ) from e

    log_sink = _write_capture_log(
        archive_root,
        date=accessed,
        question=result.title or '',
        repository=result.repository or domain_of(url),
        collection=result.collection,
        terms=result.terms,
        result=f'staged {notes_rel}',
        stub_rel=notes_rel,
    )

    print(f'Captured via {matched} ({result.source_type})')
    print(f'Staged bundle folder {_rel(folder, archive_root)}/')
    print(f'  notes.md')
    for _, dest_name, role in planned:
        print(f'  {dest_name}  (role: {role})')
    if result.people:
        print(f'Person hints: {", ".join(result.people)}')
    if log_sink == 'index':
        print('Logged the search in the index (search_log)')
    elif log_sink == 'jsonl':
        print('Logged the search in .cache/capture_log.jsonl')

    changed = [str(folder / 'notes.md')]
    changed += [str(folder / dest_name) for _, dest_name, _ in planned]
    return Result(
        exit_code=EXIT_CLEAN,
        data={'status': 'ok', 'recipe': recipe_name, 'stub': notes_rel,
              'bundle_folder': True},
        changed=changed,
    )


# ── Ingest: sweep staged bundles into the inbox (TOOLING_INGESTION §6) ──────────

# Where the browser companion (and the bookmarklet / native host) drop staged
# bundles when nothing reroutes the browser's downloads. `--ingest` sweeps from
# here into the archive's real `inbox/` - the one sanctioned move at intake.
_DEFAULT_STAGING = '~/Downloads/fha-inbox'
# Swept bundles are *parked* here, never hard-deleted (never-lose-the-human's-work).
_INGESTED_DIRNAME = '.ingested'


def _resolve_staging_dir(staging_arg: str | None, fha_config: dict) -> Path:
    """Resolve the staging folder: explicit arg → fha.yaml `capture_staging:` → default.

    `capture_staging` is *not* an archive root (it lives under the browser's
    Downloads tree, outside the archive), so it is read straight off the config
    and `~`-expanded - never routed through `resolve_path`, which would anchor it
    under the archive root.
    """
    if staging_arg:
        return Path(staging_arg).expanduser().resolve()
    configured = fha_config.get('capture_staging')
    if configured:
        return Path(str(configured)).expanduser().resolve()
    return Path(_DEFAULT_STAGING).expanduser().resolve()


def _iter_bundles(staging: Path) -> list[Path]:
    """Bundle subfolders of `staging`, excluding the `.ingested/` parking lot.

    Sorted by name so a sweep is deterministic (the `<slug>-<timestamp>` naming
    makes that chronological in practice).
    """
    return sorted(
        d for d in staging.iterdir()
        if d.is_dir() and d.name != _INGESTED_DIRNAME
    )


def staged_bundles(fha_config: dict, staging_dir: str | None = None) -> tuple[Path, list[Path]]:
    """Resolve the staging folder and list its un-ingested bundles.

    The shared discovery used by both `run_ingest` (to sweep) and `fha doctor`
    (to nudge "you have N captures waiting"). A bundle whose name is already
    parked in `.ingested/` is excluded, so the count reflects real outstanding
    work. Returns `(staging_dir, [])` when the folder doesn't exist yet.
    """
    staging = _resolve_staging_dir(staging_dir, fha_config)
    if not staging.is_dir():
        return staging, []
    ingested = staging / _INGESTED_DIRNAME
    return staging, [b for b in _iter_bundles(staging) if not (ingested / b.name).exists()]


def _read_bundle(bundle: Path) -> tuple[dict, str, list[tuple[Path, str]]]:
    """Read a staged bundle (§3 contract): `capture.json` + `page.html` + assets.

    Returns `(capture_json, page_html, assets)` where `assets` is a list of
    `(path, role)` pairs in capture.json order (empty for a pointer-only
    capture). Both capture.json schemas are read:

      • schema 2: an `assets: [{file, role, mode, provisional?}]` list - one entry
        per staged file, each with its role (`webpage` for the page copy,
        `record`/`front` for the evidence).
      • schema 1: the flat `asset_mode` / `asset_file` pair (one asset, or none),
        given the default `record` role.

    `run_ingest` then files zero/one asset as a lone-sidecar stub and two-or-more
    as a §12.1 bundle folder.

    The scrape source is `page.html` (the always-saved raw DOM, §3) when present;
    if a bundle omits it, the `webpage`-role HTML asset (the single-file snapshot)
    is parsed instead - the recipe reads title/canonical/meta/JSON-LD/tables, all
    of which a JSON-LD-preserving snapshot still carries. This keeps ingest
    working for a snapshot-only bundle without dropping the §3 page.html contract
    for the bundles that ship it.

    Raises BundleError (left-in-place, reported) when the bundle is malformed:
    no parseable HTML scrape source at all, a missing/unreadable/invalid
    `capture.json`, or an unreadable page/snapshot file - a browser still
    holding `page.html` open (common on Windows) must skip THIS bundle with a
    close-the-program next step, never abort the sibling bundles' sweep.
    """
    cap_path = bundle / 'capture.json'
    if not cap_path.is_file():
        raise BundleError("missing capture.json")
    try:
        # ValueError catches BOTH json.JSONDecodeError (a ValueError) and a
        # UnicodeDecodeError from non-UTF-8 bytes - the latter is NOT an OSError,
        # so a mis-encoded capture.json must be converted to BundleError here or
        # it escapes uncaught and aborts the whole sweep.
        cap = json.loads(cap_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise BundleError(f"could not read capture.json: {e}") from e
    if not isinstance(cap, dict):
        raise BundleError("capture.json is not a JSON object")

    # Forgiving schema check (never refuse): a newer companion may add fields a
    # current tool doesn't know - read what we share, nudge the human to update.
    # Accept int OR float (`2.0`) since JSON numbers often deserialize as float.
    schema = cap.get('schema')
    if isinstance(schema, (int, float)) and not isinstance(schema, bool) \
            and schema > _CAPTURE_JSON_SCHEMA:
        print(f'WARNING: bundle {bundle.name} declares capture.json schema '
              f'{schema}, newer than this tool reads ({_CAPTURE_JSON_SCHEMA}); '
              f'filing the fields it shares. Run `fha update-tools` if anything '
              f'looks missing.', file=sys.stderr)

    # Normalize the fields that flow into run_capture so a type-malformed-but-
    # JSON-valid value (a number `title`, a `null` in `people`) can't crash the
    # engine mid-sweep - that would abort sibling bundles, breaking the resilient
    # contract. Scalars are str()-coerced (forgiving: `1880` → "1880"); a
    # list/dict where text belongs is structurally wrong → reported BundleError.
    for key in ('url', 'title', 'source_type', 'source_date', 'accessed', 'notes',
                'repository'):
        val = cap.get(key)
        if val is None or isinstance(val, str):
            continue
        if isinstance(val, (list, dict)):
            raise BundleError(
                f"capture.json '{key}' must be text, got {type(val).__name__}")
        cap[key] = str(val)
    people = cap.get('people')
    if people is not None:
        if not isinstance(people, list):
            people = [people]
        # Drop nulls/nested structures; str()-coerce the rest (numbers etc.).
        cap['people'] = [str(x) for x in people
                         if x is not None and not isinstance(x, (list, dict))]

    assets = _resolve_bundle_assets(bundle, cap)

    # Scrape source: the raw page.html when shipped, else the webpage-role HTML
    # snapshot (single-file snapshots preserve JSON-LD/meta, so they parse), else
    # the first .html/.htm asset. A bundle with neither has no scrape source.
    # These reads get the same OSError -> BundleError treatment as capture.json:
    # a permission-denied/locked file (the browser still writing or holding it,
    # the common Windows case) is this one bundle's problem, not the sweep's.
    page = bundle / 'page.html'
    if page.is_file():
        html = _read_scrape_source(page)
    else:
        scrape = _scrape_source_from_assets(assets)
        if scrape is None:
            raise BundleError(
                "no scrape source: bundle has neither page.html nor an HTML asset")
        html = _read_scrape_source(scrape)
    return cap, html, assets


def _read_scrape_source(path: Path) -> str:
    """Read a bundle's HTML scrape source, turning an OSError into BundleError.

    The bytes decode with errors='replace' (a stray byte must not kill a
    capture), but the read itself can fail outright - a locked or permission-
    denied `page.html` while the browser still holds it is the normal Windows
    failure. That must surface as a reported, left-in-place bundle with a plain
    next step, not an OSError traceback that aborts every sibling bundle.
    """
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        raise BundleError(
            f"could not read {path.name} ({e}) - close the program that is "
            "using it (or recapture the page), then re-run `fha capture "
            "--ingest`") from e


def _scrape_source_from_assets(assets: list[tuple[Path, str]]) -> Path | None:
    """Pick the HTML asset to parse when a bundle omits the raw page.html.

    Prefers the `webpage`-role asset (the page snapshot); falls back to the first
    `.html`/`.htm` asset. Returns None when no asset is HTML.
    """
    html_exts = ('.html', '.htm', '.xhtml')
    for path, role in assets:
        if role == 'webpage' and path.suffix.lower() in html_exts:
            return path
    for path, _role in assets:
        if path.suffix.lower() in html_exts:
            return path
    return None


def _bundle_file(bundle: Path, named) -> Path | None:
    """Resolve a capture.json-named asset file inside a bundle, if it exists.

    Names are taken as relative to the bundle and confined to it (a `..` or
    absolute path that escapes the bundle is ignored), since a staged bundle's
    asset must live inside the bundle the sweep is reading. `page.html` and
    `capture.json` are never returned as assets - they are the bundle scaffolding.
    """
    if not named:
        return None
    base = Path(str(named)).name
    if base in ('page.html', 'capture.json'):
        return None
    cand = bundle / base
    return cand if cand.is_file() else None


def _resolve_bundle_assets(bundle: Path, cap: dict) -> list[tuple[Path, str]]:
    """Resolve a bundle's assets to an ordered list of `(path, role)` pairs.

    Schema 2 (`assets:` list) and schema 1 (`asset_mode`/`asset_file`) both flow
    through here. Order is preserved (record-then-webpage as the panel emits, so
    the record evidence is the natural primary). Roles are normalized to a string;
    an asset with no role becomes the default `record`.
    """
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()

    assets = cap.get('assets')
    if isinstance(assets, list):
        for item in assets:
            if not isinstance(item, dict):
                continue
            declared = item.get('file')
            # The scaffolding files are not assets (a producer that lists one is
            # harmlessly ignored, not treated as a missing-asset error).
            if declared and Path(str(declared)).name in ('page.html', 'capture.json'):
                continue
            path = _bundle_file(bundle, declared)
            if path is None:
                # A file listed in capture.json is part of the completed-capture
                # contract; if it's missing (interrupted download, renamed
                # payload) the bundle is malformed - report it and leave it in
                # staging rather than silently filing an incomplete capture.
                if declared:
                    raise BundleError(
                        f"declared asset {str(declared)!r} is missing from the bundle")
                continue
            if path.name in seen:
                continue
            role = str(item.get('role') or '').strip().lower() or _DEFAULT_ASSET_ROLE
            out.append((path, role))
            seen.add(path.name)
        return out

    # ── schema 1 fallback: the flat asset_mode / asset_file pair ───────────────
    if cap.get('asset_mode') == 'none':
        return []
    asset = _bundle_file(bundle, cap.get('asset_file'))
    if asset is None:
        # Fall back to any file named `asset.<ext>` (asset.jpg/.pdf/.html, and
        # multi-extension like asset.tar.gz - `startswith` catches what a
        # `stem == 'asset'` test would miss; a bare extensionless `asset` is
        # skipped since it can't be staged with a suffix).
        for p in sorted(bundle.iterdir()):
            if p.is_file() and p.name.startswith('asset.'):
                asset = p
                break
    return [(asset, _DEFAULT_ASSET_ROLE)] if asset is not None else []


def _park_ingested(bundle: Path, ingested_dir: Path) -> None:
    """Move a swept bundle into `.ingested/` (never hard-delete)."""
    ingested_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(bundle), str(ingested_dir / bundle.name))


def run_ingest(
    archive_root: Path,
    fha_config: dict,
    *,
    staging_dir: str | None,
    dry_run: bool = False,
) -> Result:
    """Sweep staged capture bundles into the inbox (TOOLING_INGESTION §6).

    Each `<slug>-<timestamp>/` bundle is fed through `run_capture` wholesale -
    `page.html` as the HTML, the asset as `--asset`, the `capture.json` fields as
    explicit overrides - so the stub is byte-identical to the paste-fallback's.
    On success the bundle is parked in `.ingested/`. The sweep is idempotent
    (a name already parked is skipped) and resilient (a malformed bundle is
    reported and left in place, never aborting its siblings).
    """
    staging = _resolve_staging_dir(staging_dir, fha_config)
    if not staging.is_dir():
        print(f'No staging folder at {staging} - nothing to ingest.')
        print('Capture bundles land there from the browser companion; '
              'point --ingest at a folder or set capture_staging: in fha.yaml.')
        return Result(exit_code=EXIT_CLEAN, data={'status': 'no-staging', 'ingested': 0})

    ingested_dir = staging / _INGESTED_DIRNAME
    bundles = _iter_bundles(staging)
    if not bundles:
        print(f'No staged bundles in {staging}.')
        return Result(exit_code=EXIT_CLEAN, data={'status': 'empty', 'ingested': 0})

    changed: list[str] = []
    outcomes: list[dict] = []
    ingested = 0
    skipped = 0
    failed = 0
    park_failed = 0

    for bundle in bundles:
        name = bundle.name
        if (ingested_dir / name).exists():
            print(f'Skipping {name} - already ingested (in {_INGESTED_DIRNAME}/).')
            skipped += 1
            outcomes.append({'bundle': name, 'status': 'skipped'})
            continue
        try:
            cap, html, assets = _read_bundle(bundle)
        except BundleError as e:
            print(f'WARNING: skipping malformed bundle {name}: {e}', file=sys.stderr)
            print(f'         left in place at {bundle}', file=sys.stderr)
            failed += 1
            outcomes.append({'bundle': name, 'status': 'malformed', 'error': str(e)})
            continue

        # cap fields are already type-normalized by _read_bundle.
        prefix = '[dry-run] ' if dry_run else ''
        print(f'{prefix}Ingesting bundle {name}:')
        try:
            # SPEC §12.1: zero/one asset → a lone-sidecar stub; two-or-more → a
            # bundle FOLDER (the schema-2 "both" case). Both paths re-extract from
            # page.html and apply the same capture.json overrides.
            if len(assets) >= 2:
                result = _ingest_bundle_folder(
                    archive_root, fha_config,
                    url=cap.get('url'),
                    title=cap.get('title'),
                    source_type=cap.get('source_type'),
                    source_date=cap.get('source_date'),
                    html=html,
                    accessed=cap.get('accessed'),
                    notes=cap.get('notes'),
                    people=cap.get('people'),
                    repository=cap.get('repository'),
                    assets=assets,
                    dry_run=dry_run,
                )
            else:
                result = run_capture(
                    archive_root, fha_config,
                    url=cap.get('url'),
                    title=cap.get('title'),
                    source_type=cap.get('source_type'),
                    source_date=cap.get('source_date'),
                    asset=assets[0][0] if assets else None,
                    html=html,
                    accessed=cap.get('accessed'),
                    notes=cap.get('notes'),
                    people=cap.get('people'),
                    repository=cap.get('repository'),
                    dry_run=dry_run,
                )
        except (CaptureError, CaptureWriteError) as e:
            print(f'WARNING: could not ingest {name}: {e}', file=sys.stderr)
            print(f'         left in place at {bundle}', file=sys.stderr)
            failed += 1
            outcomes.append({'bundle': name, 'status': 'failed', 'error': str(e)})
            continue
        except Exception as e:  # noqa: BLE001
            # Resilient-ingest contract: one bundle must never abort the batch.
            # _read_bundle normalizes known fields, but a recipe or an unforeseen
            # input could still raise - report this bundle and keep sweeping
            # rather than crashing out of the loop (and past the CLI guard).
            print(f'WARNING: could not ingest {name}: unexpected error: {e}',
                  file=sys.stderr)
            print(f'         left in place at {bundle}', file=sys.stderr)
            failed += 1
            outcomes.append({'bundle': name, 'status': 'failed', 'error': str(e)})
            continue

        changed.extend(result.changed)
        if dry_run:
            print(f'[dry-run] Would park {name} in {_INGESTED_DIRNAME}/')
            outcomes.append({'bundle': name, 'status': 'dry-run',
                             'stub': result.data.get('stub')})
            continue
        # The stub is now filed. A failed park does NOT un-file it, so the bundle
        # counts as ingested - but warn, because the un-parked bundle would be
        # re-swept (and duplicated) on the next run until moved by hand.
        parked_ok = True
        try:
            _park_ingested(bundle, ingested_dir)
        except OSError as e:
            parked_ok = False
            park_failed += 1
            print(f'WARNING: filed {name} into the inbox but could not park the '
                  f'bundle in {_INGESTED_DIRNAME}/: {e}', file=sys.stderr)
            print(f'         the stub IS filed - move or delete {bundle} by hand '
                  f'so a re-run does not ingest it again.', file=sys.stderr)
        ingested += 1
        outcomes.append({'bundle': name,
                         'status': 'ingested' if parked_ok else 'ingested-not-parked',
                         'stub': result.data.get('stub')})

    verb = 'Would ingest' if dry_run else 'Ingested'
    summary = f'{verb} {len(bundles) - skipped - failed} bundle(s)'
    if skipped:
        summary += f', skipped {skipped} already-ingested'
    if park_failed:
        summary += f', {park_failed} filed but not parked (bundle left in staging)'
    if failed:
        summary += f', {failed} left in place (see warnings)'
    print(summary + '.')

    return Result(
        exit_code=EXIT_ERRORS if (failed or park_failed) else EXIT_CLEAN,
        data={'status': 'ok', 'ingested': ingested, 'skipped': skipped,
              'failed': failed, 'park_failed': park_failed, 'bundles': outcomes},
        changed=changed,
    )


# ── Native-messaging host (TOOLING_INGESTION §5.7) ─────────────────────────────
#
# The browser launches `fha capture --host` on demand (no resident daemon) and
# exchanges length-prefixed JSON over stdin/stdout. One write (file a bundle
# straight into inbox/, reusing run_ingest) and two read-only queries (name
# autocomplete, already-captured check) - everything against the local archive,
# nothing published (§7). The extension's `nativeHost.sendBundle()` already
# speaks this; only this backend was missing.

_NATIVE_HOST_NAME = 'com.plaintext.fha_capture'
_NATIVE_HOST_PROTOCOL = 1
# Cap a frame so a garbled length prefix can't make the host allocate wildly; a
# bundle with a base64 asset or two is a few MB, 64 MiB is comfortable headroom.
_NATIVE_MAX_FRAME = 64 * 1024 * 1024


def _read_native_message(stream) -> dict | None:
    """One length-prefixed JSON message from `stream`, or `None` at clean EOF.

    Native-messaging framing: a 4-byte unsigned length in native byte order,
    then that many UTF-8 JSON bytes. A short/oversized/garbled frame raises
    `BundleError` rather than hanging or over-allocating.
    """
    raw_len = stream.read(4)
    if not raw_len:
        return None
    if len(raw_len) < 4:
        raise BundleError('native message length prefix truncated')
    (length,) = struct.unpack('@I', raw_len)
    if length == 0:
        return {}
    if length > _NATIVE_MAX_FRAME:
        raise BundleError(f'native message too large ({length} bytes)')
    payload = stream.read(length)
    if len(payload) < length:
        raise BundleError('native message body truncated')
    return json.loads(payload.decode('utf-8'))


def _write_native_message(stream, obj: dict) -> None:
    """Write `obj` as one length-prefixed JSON native message and flush."""
    data = json.dumps(obj).encode('utf-8')
    stream.write(struct.pack('@I', len(data)))
    stream.write(data)
    stream.flush()


def _safe_member_name(name: str | None, default: str) -> str:
    """A browser-supplied bundle/file name reduced to a single safe path segment."""
    base = os.path.basename((name or '').replace('\\', '/').strip())
    base = re.sub(r'[^A-Za-z0-9._-]', '-', base).strip('.-')
    return base or default


def _host_ingest(archive_root: Path, fha_config: dict, msg: dict) -> dict:
    """Materialize the framed bundle and file it through the normal ingest path.

    The transport is the only new thing: write `page.html` + `capture.json` +
    the base64 assets into a temp staging dir and call `run_ingest` wholesale, so
    the stub is byte-identical to the `--ingest` / paste-fallback path (§6 seam).
    """
    capture_json = msg.get('captureJson')
    if capture_json in (None, ''):
        return {'ok': False, 'error': 'missing captureJson'}
    bundle_name = _safe_member_name(msg.get('bundleName'), f'capture-{_today()}')
    with tempfile.TemporaryDirectory(prefix='fha-host-') as tmp:
        bundle = Path(tmp) / bundle_name
        bundle.mkdir(parents=True)
        page_html = msg.get('pageHtml')
        if page_html is not None:
            (bundle / 'page.html').write_text(page_html, encoding='utf-8')
        text = capture_json if isinstance(capture_json, str) else json.dumps(capture_json)
        (bundle / 'capture.json').write_text(text, encoding='utf-8')
        for asset in (msg.get('assets') or []):
            if not isinstance(asset, dict):
                return {'ok': False, 'error': 'each asset must be an object'}
            fn = _safe_member_name(asset.get('filename'), '')
            if not fn:
                continue
            b64 = asset.get('base64')
            # Strip transport whitespace, then require real data: an absent/blank
            # value must not silently file a zero-byte record image as success.
            stripped = re.sub(r'\s', '', b64) if isinstance(b64, str) else ''
            if not stripped:
                return {'ok': False, 'error': f'asset {fn!r} has no data'}
            try:
                data = base64.b64decode(stripped, validate=True)
            except (ValueError, TypeError):
                return {'ok': False, 'error': f'asset {fn!r} is not valid base64'}
            (bundle / fn).write_bytes(data)
        try:
            result = run_ingest(archive_root, fha_config, staging_dir=str(Path(tmp)))
        except CaptureWriteError as e:
            return {'ok': False, 'error': str(e)}
    for outcome in result.data.get('bundles', []):
        if outcome.get('bundle') == bundle_name:
            if outcome.get('stub'):
                return {'ok': True, 'stub': outcome['stub']}
            return {'ok': False, 'error': outcome.get('error') or 'bundle could not be filed'}
    return {'ok': False, 'error': 'capture was not filed'}


def _host_suggest_names(archive_root: Path, fha_config: dict, q: str | None, limit: int) -> dict:
    """Archive person names + aliases matching `q` (read-only, suggestion-only)."""
    query = (q or '').strip().lower()
    people_dir = resolve_path('people', fha_config, archive_root)
    names: list[str] = []
    seen: set[str] = set()
    if people_dir.is_dir():
        for md in sorted(people_dir.rglob('*.md')):
            if md.name.startswith('_'):                  # templates
                continue
            try:
                meta = (read_record(md).get('meta') or {})
            except Exception:                            # noqa: BLE001 - skip a bad file
                continue
            candidates = [str(meta['name'])] if meta.get('name') else []
            for alias in (meta.get('aliases') or []):
                alias = str(alias)
                if not re.match(r'^[PSLC]-[A-Za-z0-9]+$', alias):  # skip id aliases
                    candidates.append(alias)
            for name in candidates:
                key = name.lower()
                if key in seen or (query and query not in key):
                    continue
                seen.add(key)
                names.append(name)
    names.sort(key=lambda n: (not n.lower().startswith(query), n.lower()))
    return {'ok': True, 'names': names[:max(0, limit)]}


# Query params that carry the durable record identity (not per-visit chrome) and
# so must survive normalization. Newspapers.com clippings share one image path
# and differ only by `clipping_id`, so dropping the whole query would conflate
# two distinct clips.
_DURABLE_QUERY_KEYS = ('clipping_id',)


def _normalize_source_url(url: str | None) -> str:
    """`host+path(+durable id)` for dup checks: www- and trailing-slash-stripped,
    per-visit query dropped but a durable record id kept.

    Ancestry/FamilySearch URLs carry throwaway params (`_phsrc`, `pId`, …) that
    change each visit; the stable record id usually lives in the path. But a
    Newspapers.com clip's identity is the `clipping_id` query param, so that one
    is preserved - otherwise two clippings off the same image page collapse to
    one and a valid new capture is wrongly reported already-captured.
    """
    p = urlparse(url or '')
    host = (p.netloc or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    if not host:
        return ''
    path = (p.path or '').rstrip('/')
    qs = parse_qs(p.query)
    ids = [f'{k}={qs[k][0]}' for k in _DURABLE_QUERY_KEYS if qs.get(k)]
    return f'{host}{path}' + ('?' + '&'.join(sorted(ids)) if ids else '')


def _host_check_url(archive_root: Path, fha_config: dict, url: str | None) -> dict:
    """Whether a source URL is already captured (by normalized host+path).

    Scans the *configured* sources/inbox roots (an archive may map either outside
    the tree via fha.yaml), so a URL already staged in a relocated inbox is found.
    """
    target = _normalize_source_url(url)
    if not target:
        return {'ok': True, 'known': False}
    for sub in ('sources', 'inbox'):
        root = resolve_path(sub, fha_config, archive_root)
        if not root.is_dir():
            continue
        for md in sorted(root.rglob('*.md')):
            try:
                meta = (read_record(md).get('meta') or {})
            except Exception:                            # noqa: BLE001
                continue
            for link in (meta.get('external_links') or []):
                href = link.get('url') if isinstance(link, dict) else link
                if href and _normalize_source_url(href) == target:
                    return {'ok': True, 'known': True,
                            'source': meta.get('id'),
                            'date': meta.get('source_date') or meta.get('created')}
    return {'ok': True, 'known': False}


def _host_dispatch(archive_root: Path, fha_config: dict, msg: dict) -> dict:
    """Route one native-messaging request to its handler (unknown → clean error)."""
    action = (msg.get('action') or msg.get('type') or '').strip()
    if action == 'ping':
        return {'ok': True, 'v': _NATIVE_HOST_PROTOCOL}
    if action in ('ingest', 'file'):
        return _host_ingest(archive_root, fha_config, msg)
    if action == 'suggestNames':
        try:
            limit = int(msg.get('limit') or 8)
        except (TypeError, ValueError):
            limit = 8
        return _host_suggest_names(archive_root, fha_config, msg.get('q'), limit)
    if action == 'checkUrl':
        return _host_check_url(archive_root, fha_config, msg.get('url'))
    return {'ok': False, 'error': f'unsupported action: {action!r}'}


def run_host(archive_root: Path, fha_config: dict, *, stdin=None, stdout=None) -> int:
    """Serve native-messaging requests until EOF (one read, one query, no daemon)."""
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout.buffer
    while True:
        try:
            msg = _read_native_message(stdin)
        except (BundleError, ValueError, json.JSONDecodeError) as e:
            try:
                _write_native_message(stdout, {'ok': False, 'error': str(e)})
            except OSError:  # browser already closed the pipe
                return EXIT_CLEAN
            return EXIT_ERRORS
        if msg is None:
            return EXIT_CLEAN
        try:
            # Native-messaging stdout must carry ONLY framed messages, but a
            # handler (run_ingest) prints progress lines. `stdout` above is the
            # real binary channel, captured before this redirect, so routing any
            # stray stdout to stderr keeps the protocol uncorrupted.
            with contextlib.redirect_stdout(sys.stderr):
                resp = _host_dispatch(archive_root, fha_config, msg)
        except Exception as e:  # noqa: BLE001 - a single request must never crash the host
            resp = {'ok': False, 'error': f'unexpected error: {e}'}
        try:
            _write_native_message(stdout, resp)
        except OSError:  # browser closed stdout - exit quietly, not with a traceback
            return EXIT_CLEAN


def _install_host(archive_root: Path, *, extension_id: str | None,
                  manifest_dir: str | None, dry_run: bool = False,
                  browser: str = 'chrome') -> int:
    """Write the native-messaging manifest (+ launcher) registering this CLI.

    `manifest_dir` overrides the per-OS native-messaging location (used by tests
    and for a non-default browser profile); `browser` ('chrome' | 'edge') picks
    the default location and registry key otherwise. The launcher is a tiny
    wrapper that invokes this interpreter on `tools/fha.py` with `capture --host
    --root <archive>`. `dry_run` previews the paths without writing anything.
    """
    # Chrome/Edge require the manifest `path` (and so the manifest dir) to be
    # ABSOLUTE on every OS; a relative --host-manifest-dir would otherwise write
    # a host the browser can't launch.
    target_dir = (Path(manifest_dir).expanduser().resolve()
                  if manifest_dir else _native_manifest_dir(browser))

    # The CLI is run as `tools/fha.py` (there is no bare `fha` executable in the
    # repo/installed layout); point the launcher at the real entrypoint.
    fha_entry = Path(__file__).resolve().parent / 'fha.py'
    archive_root = Path(archive_root).expanduser().resolve()
    ext = '.bat' if os.name == 'nt' else '.sh'
    launcher = target_dir / f'fha-capture-host{ext}'
    manifest_path = target_dir / f'{_NATIVE_HOST_NAME}.json'

    origin = f'chrome-extension://{extension_id}/' if extension_id else \
        'chrome-extension://REPLACE_WITH_EXTENSION_ID/'
    manifest = {
        'name': _NATIVE_HOST_NAME,
        'description': 'Plaintext Family History capture host',
        'path': str(launcher),
        'type': 'stdio',
        'allowed_origins': [origin],
    }

    if dry_run:
        print(f'[dry-run] would write native-messaging manifest → {manifest_path}')
        print(f'[dry-run] would write launcher → {launcher}')
        return EXIT_CLEAN

    target_dir.mkdir(parents=True, exist_ok=True)
    # The browser appends args when it launches the host (the calling extension's
    # origin on every platform, plus `--parent-window=<HWND>` on Windows). The
    # host reads only stdin/stdout, so the launcher must NOT forward them - the
    # CLI parses strictly and an unrecognized positional would abort host startup.
    if os.name == 'nt':
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{fha_entry}" capture --host '
            f'--root "{archive_root}"\r\n', encoding='utf-8')
    else:
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{fha_entry}" capture --host '
            f'--root "{archive_root}"\n', encoding='utf-8')
        os.chmod(launcher, 0o755)

    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(f'Wrote native-messaging manifest → {manifest_path}')
    print(f'Launcher → {launcher}')
    if os.name == 'nt':
        # Chrome/Edge discover Windows hosts via the registry, not a directory
        # scan, so the manifest alone is not enough - point the user at the key
        # for the chosen browser.
        reg_vendor = 'Microsoft\\Edge' if browser == 'edge' else 'Google\\Chrome'
        print('NOTE: on Windows the browser finds the host through the registry. '
              'Register it with:\n'
              f'  REG ADD "HKCU\\Software\\{reg_vendor}\\NativeMessagingHosts\\'
              f'{_NATIVE_HOST_NAME}" /ve /t REG_SZ /d "{manifest_path}" /f')
    if not extension_id:
        print('NOTE: edit "allowed_origins" to your extension id '
              '(chrome://extensions → the companion → ID), or re-run with '
              '--extension-id.')
    return EXIT_CLEAN


def _native_manifest_dir(browser: str = 'chrome') -> Path:
    """The per-OS native-messaging hosts directory for `browser` (chrome|edge)."""
    home = Path.home()
    if browser == 'edge':
        if os.name == 'nt':
            return Path(os.environ.get('LOCALAPPDATA', home)) / 'Microsoft' / \
                'Edge' / 'User Data' / 'NativeMessagingHosts'
        if sys.platform == 'darwin':
            return home / 'Library' / 'Application Support' / 'Microsoft Edge' / \
                'NativeMessagingHosts'
        return home / '.config' / 'microsoft-edge' / 'NativeMessagingHosts'
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', home)) / 'Google' / 'Chrome' / \
            'User Data' / 'NativeMessagingHosts'
    if sys.platform == 'darwin':
        return home / 'Library' / 'Application Support' / 'Google' / 'Chrome' / \
            'NativeMessagingHosts'
    return home / '.config' / 'google-chrome' / 'NativeMessagingHosts'


# ── CLI ───────────────────────────────────────────────────────────────────────

# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Clip an open web-record page into your inbox to process later.

  fha capture --url URL              Capture a page (HTML piped on stdin)
  fha capture --asset saved.html     Capture from a saved page file
  fha capture --path PATH            Register a file that must never move
  fha capture --ingest               Sweep staged capture bundles into the inbox

Writes a stub in inbox/, never a finished source; a later `fha process` turns it
into a real record. It reads only the page you already have - never logs in.
--path is different: it never reads anything, it just remembers where a file
(often a photo) already lives, so it enters the queue without being moved."""


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'capture',
        help='Capture an open web record page into an inbox source stub',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(p)
    p.set_defaults(func=_run_capture)


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.add_argument('--url', metavar='URL', help='Source page URL (the record being captured)')
    p.add_argument('--title', metavar='TITLE', help='Override the captured title')
    p.add_argument('--type', metavar='TYPE', dest='source_type',
                   help='Override the inferred source_type (controlled vocabulary)')
    p.add_argument('--date', metavar='DATE', dest='source_date',
                   help="Override the source's own date, e.g. 1880 or 'about 1880'")
    p.add_argument('--asset', metavar='FILE',
                   help='Asset to stage alongside the stub (an image, or the saved page HTML)')
    p.add_argument('--path', metavar='PATH',
                   help='Register a file that must stay exactly where it is (a photo '
                        'library is never reorganized) - writes a pointer stub only, '
                        'never moves, renames, or reads the file itself')
    p.add_argument('--note', metavar='TEXT',
                   help='A note to record with --path (goes in the stub body)')
    p.add_argument('--ingest', nargs='?', const=True, default=False, metavar='DIR',
                   help='Sweep staged capture bundles from DIR (default: the '
                        'capture_staging folder or ~/Downloads/fha-inbox) into the inbox')
    p.add_argument('--host', action='store_true',
                   help='Run as the browser native-messaging host (stdin/stdout '
                        'framed JSON); launched by the companion, not by hand')
    p.add_argument('--install-host', action='store_true',
                   help='Register the native-messaging host manifest for the browser')
    p.add_argument('--extension-id', metavar='ID',
                   help='Companion extension id for --install-host allowed_origins')
    p.add_argument('--host-manifest-dir', metavar='DIR',
                   help='Override where --install-host writes the manifest (default: '
                        "the browser's native-messaging hosts dir)")
    p.add_argument('--browser', choices=('chrome', 'edge'), default='chrome',
                   help='Target browser for --install-host paths/registry (default: chrome)')
    p.add_argument('--dry-run', action='store_true', help='Preview without writing')


# Each capture mode owns a set of flags. Mode precedence (highest first):
# --install-host, --host, --ingest, --path, then the default page capture.
# Mixing flags from two modes is almost always a copy-paste mistake (a stray
# --url left on an --ingest command line); the old code silently ran the
# winning mode and dropped the loser's flags without a word. Refuse the
# combination up front instead, naming both sides so the fix is obvious.
# --browser (defaulted, only read by --install-host) and --dry-run
# (cross-mode; --host keeps its own refusal) are deliberately not mode-owned
# here.
_CAPTURE_MODE_NOUN = {
    'install-host': 'the host installer',
    'host': 'the native-messaging host',
    'ingest': 'the staging sweep',
    'path': 'registering a file that must never move',
    'page capture': 'a page capture',
}

# (attr, display flag, owning mode) in a stable reporting order. `mode` names
# the noun used in the conflict message; a flag legitimately shared by more
# than one mode (only --title so far: it labels both a captured page and a
# --path pointer stub) is widened via _SHARED_FLAG_MODES below rather than
# duplicated here, so its message wording stays exactly what it was before
# --path existed whenever the OTHER mode (page capture) is the one refusing.
_CAPTURE_FLAG_MODES = [
    ('install_host', '--install-host', 'install-host'),
    ('extension_id', '--extension-id', 'install-host'),
    ('host_manifest_dir', '--host-manifest-dir', 'install-host'),
    ('host', '--host', 'host'),
    ('ingest', '--ingest', 'ingest'),
    ('path', '--path', 'path'),
    ('note', '--note', 'path'),
    ('url', '--url', 'page capture'),
    ('title', '--title', 'page capture'),
    ('source_type', '--type', 'page capture'),
    ('source_date', '--date', 'page capture'),
    ('asset', '--asset', 'page capture'),
]

# Flags allowed in more than one mode without tripping the conflict check.
# --title overrides a captured page's title AND labels a --path pointer stub
# (TOOLING §13b: "optional title:") - the same human intent ("call this...")
# either way, so it is not page-capture-exclusive the way --url/--asset are.
_SHARED_FLAG_MODES: dict[str, frozenset[str]] = {
    'title': frozenset({'page capture', 'path'}),
}


def _flag_owning_modes(attr: str, primary_mode: str) -> frozenset[str]:
    """The set of modes `attr` is allowed in - usually just its primary mode."""
    return _SHARED_FLAG_MODES.get(attr, frozenset({primary_mode}))


def _flag_given(args: argparse.Namespace, attr: str) -> bool:
    """True when a capture flag was explicitly supplied (past its default)."""
    # --ingest defaults to False (bare = True, DIR = str); the rest default to
    # None (options) or False (store_true), so "not in (None, False)" covers all.
    return getattr(args, attr, None) not in (None, False)


def _mode_conflict_error(args: argparse.Namespace) -> str | None:
    """Return a plain refusal if flags from two capture modes were mixed, else None.

    The winning mode is whichever the dispatch would actually run (precedence
    order above). Any explicitly-given flag not ALLOWED in the winning mode
    (see `_flag_owning_modes`) is a conflict: report the first such flag,
    naming both the losing flag's mode and the winning mode, and tell the
    human to run the two as separate commands.
    """
    if _flag_given(args, 'install_host'):
        winner, trigger = 'install-host', '--install-host'
    elif _flag_given(args, 'host'):
        winner, trigger = 'host', '--host'
    elif _flag_given(args, 'ingest'):
        winner, trigger = 'ingest', '--ingest'
    elif _flag_given(args, 'path'):
        winner, trigger = 'path', '--path'
    else:
        winner, trigger = 'page capture', None

    for attr, display, mode in _CAPTURE_FLAG_MODES:
        if winner in _flag_owning_modes(attr, mode):
            continue
        if _flag_given(args, attr):
            trigger_note = f' ({trigger})' if trigger else ''
            return (
                f'{display} belongs to {_CAPTURE_MODE_NOUN[mode]}, but '
                f'{_CAPTURE_MODE_NOUN[winner]} is what this command is doing'
                f'{trigger_note}. Run them as two separate commands.'
            )
    return None


def _run_capture(args: argparse.Namespace) -> int:
    # resolve_root_arg carries the archive guard: a typo'd --root used to
    # stage stubs into `<typo>/inbox` with exit 0 (round-2 finding 10). The
    # command name keeps the refusal exact on the standalone-main path too.
    archive_root = resolve_root_arg(args, command='fha capture')
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    conflict = _mode_conflict_error(args)
    if conflict is not None:
        print(f'ERROR: {conflict}', file=sys.stderr)
        return EXIT_ERRORS

    # The native-messaging host and its installer are distinct modes.
    if getattr(args, 'install_host', False):
        try:
            return _install_host(
                archive_root,
                extension_id=getattr(args, 'extension_id', None),
                manifest_dir=getattr(args, 'host_manifest_dir', None),
                dry_run=getattr(args, 'dry_run', False),
                browser=getattr(args, 'browser', 'chrome'),
            )
        except OSError as e:
            # The browser's native-messaging dir may be missing/protected; report
            # the path and bail cleanly instead of dumping a traceback.
            print(f'ERROR: could not write the native-messaging host: {e}',
                  file=sys.stderr)
            return EXIT_FAILURE
    if getattr(args, 'host', False):
        if getattr(args, 'dry_run', False):
            # The host is a live server: its `ingest` action files real bundles
            # into inbox/. There is nothing to preview, so honor --dry-run's
            # no-mutation contract by refusing the combination up front.
            print('ERROR: --dry-run is not compatible with --host (the native '
                  'host files live capture bundles into the inbox; there is '
                  'nothing to preview).', file=sys.stderr)
            return EXIT_FAILURE
        return run_host(archive_root, fha_config)

    # The --ingest sweep is a distinct mode: it reads staged bundles, not stdin.
    ingest = getattr(args, 'ingest', False)
    if ingest:
        staging_dir = ingest if isinstance(ingest, str) else None
        try:
            return run_ingest(
                archive_root, fha_config,
                staging_dir=staging_dir,
                dry_run=bool(getattr(args, 'dry_run', False)),
            ).exit_code
        except CaptureWriteError as e:
            print(f'ERROR: {e}', file=sys.stderr)
            return EXIT_FAILURE

    # --path is a distinct mode: no stdin/HTML read at all, just a pointer
    # stub recording where an asset that must never move already lives.
    path_arg = getattr(args, 'path', None)
    if path_arg:
        try:
            path_result = run_capture_path(
                archive_root, fha_config,
                path=path_arg, note=getattr(args, 'note', None),
                title=getattr(args, 'title', None),
                dry_run=bool(getattr(args, 'dry_run', False)),
            )
        except CaptureWriteError as e:
            print(f'ERROR: {e}', file=sys.stderr)
            return EXIT_FAILURE
        # run_capture_path follows the house engine contract (Result, never a
        # print) - render its messages here so the terminal output is exactly
        # what it always was, just sourced from Result.messages now.
        for msg in path_result.messages:
            stream = sys.stdout if msg.level == 'info' else sys.stderr
            print(msg.text, file=stream)
        return path_result.exit_code

    asset: Path | None = None
    if args.asset:
        asset = Path(args.asset).resolve()
        if not asset.is_file():
            print(f'ERROR: --asset file not found: {args.asset}', file=sys.stderr)
            return EXIT_ERRORS

    try:
        html = _read_html(asset)
        return run_capture(
            archive_root, fha_config,
            url=args.url, title=args.title, source_type=args.source_type,
            source_date=args.source_date, asset=asset, html=html,
            dry_run=bool(getattr(args, 'dry_run', False)),
        ).exit_code
    except CaptureError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_ERRORS
    except CaptureWriteError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE


# ── Standalone ────────────────────────────────────────────────────────────────

def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha capture',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    args = parser.parse_args(argv)
    return _run_capture(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
