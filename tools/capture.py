#!/usr/bin/env python3
"""
capture.py — fha capture: the web-record intake on-ramp (TOOLING §13b).

  fha capture [--url URL] [--title "…"] [--type TYPE] [--date DATE] [--asset FILE]

Capture turns *an open web record page* into a **source stub** in `inbox/`
(SPEC §12.1) — never a finished Source. It reads the page HTML the human already
has in front of them (piped on stdin, or read from `--asset` when that file is
the saved page) and writes a `{slug}.notes.md` stub whose light frontmatter holds
the citation fields a recipe could recover and whose body is the page's visible
text. A later `fha process` session (the `process-source` skill) mints the S-id,
drafts claims, and promotes the stub into a real record — capture only *stages*.

This is the paste-fallback delivery form: it needs no browser extension, reads
only what is handed to it, and never logs in or fetches behind auth (the §13b
boundary). The browser companion is a thinner front-end onto this same backend.

Two extraction layers:

  * **Site recipes** (`tools/capture_recipes/`, M7.6/M7.7) — Ancestry,
    FamilySearch, Newspapers.com, FindAGrave each know where that site keeps the
    title, date, collection, repository, image URL, and the persons it lists.
    Recipes are *data*: a module exposing `detect(html, url)` and
    `extract(html, url)`, discovered at runtime and tried in priority order.
  * **Generic recipe** (this file) — the universal fallback for an unknown
    site: page title, canonical/`--url`, accessed-date, and visible text as the
    citation basis. Any page is capturable, just with more to fix in review.

`--title` / `--type` / `--date` always override whatever a recipe (or the
generic pass) inferred — the human's explicit word wins. Capture also writes a
research-log entry (capture is itself a logged search, closing the §16 loop):
into the live index's `search_log` when an index exists, else appended to
`.cache/capture_log.jsonl`.

Stdlib only — the page is parsed with `html.parser`, never a third-party HTML
library (the project adds no dependency before Jinja2 in M8).
"""

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  HTML parsing (stdlib html.parser — shared with the recipes)
#    _PageParser               — one pass: title / first h1 / base / canonical / meta / text
#    parse_html                — _PageParser → a ParsedPage of the fields recipes read
#    _TableParser, extract_tables — table rows as text grids (household/index tables)
#    visible_text, domain_of, meta_content, first_nonempty — recipe-facing helpers
#
#  Recipe layer
#    RecipeResult              — the normalized citation fields a recipe returns
#    generic_extract           — the universal fallback recipe
#    _load_site_recipes        — discover capture_recipes/*.py, sorted by PRIORITY
#    choose_recipe             — first recipe whose detect() matches, else generic
#
#  Stub assembly
#    _slugify / _yaml_inline   — slug + safe single-line YAML scalar
#    _render_stub              — RecipeResult + body → the inbox notes.md text
#    _unique_stub_stem         — collision-free {stem} for the stub (+ its asset)
#
#  Research log
#    _write_capture_log        — search_log row (index present) or capture_log.jsonl
#
#  Top-level + CLI
#    run_capture               — read HTML, choose recipe, write stub + asset + log
#    register / _run_capture / _standalone_main
#
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import argparse
import datetime
import functools
import importlib
import json
import pkgutil
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    SOURCE_TYPES,
    FhaConfigError,
    configure_utf8_stdout,
    is_valid_edtf,
    load_fha_yaml,
    resolve_path,
    resolve_root_arg,
)

import yaml

configure_utf8_stdout()

# The generic fallback's source_type. SPEC §14 / _lib.SOURCE_TYPES spell the
# web vocabulary term `website` (BUILD.md M7.5 writes the shorthand `web`); we
# emit the controlled-vocabulary value so the staged stub processes cleanly —
# `fha process` refuses an out-of-vocabulary source_type hint.
_GENERIC_SOURCE_TYPE = 'website'

# Visible-text body is a citation *basis*, not the whole page — cap it so a long
# article doesn't bloat every stub (BUILD.md M7.5: "visible text … ~2000 chars").
_BODY_CHAR_CAP = 2000


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
    written to the stub — they feed the research-log entry (§16).
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
    citation *basis* the reviewer refines — the source_type is the generic
    `website` and the citation is assembled from whatever the page exposed.
    """
    page = parse_html(html)
    page_url = first_nonempty(url, page.canonical, page.base_href,
                              meta_content(page, 'og:url'))
    title = first_nonempty(page.title, meta_content(page, 'og:title'), page.h1) \
        or (domain_of(page_url) or 'captured page')
    repository = domain_of(page_url)
    accessed = _today()

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
        source_date=first_nonempty(meta_content(page, 'article:published_time')),
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
    command down — an unknown page still captures via the generic fallback.
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
        except Exception as e:  # noqa: BLE001 — a broken recipe must not abort capture
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
    it — otherwise the stub's frontmatter would not re-parse on `fha process`.
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
    body — never a §14 record. `people:` lists the *names* the page showed, a
    hint the processing pass reconciles against the index; it is not the §14
    P-id `people:` list (a stub has no resolved P-ids yet).

    `has_asset` is False for TOOLING §13b case (c) — the page only points
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
    lines.append(body if body else '*(captured page — no visible text extracted)*')
    lines.append('')
    return '\n'.join(lines)


def _unique_stub_stem(inbox: Path, slug: str, asset_suffix: str | None = None) -> str:
    """Return a `{stem}` (slug, else slug-2, slug-3 …) free of a `.notes.md` clash.

    The stub and its optional asset share this stem so they pair by basename
    (SPEC §12.1 lone-sidecar rule) — so the collision check looks at the stub
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
    `.cache/capture_log.jsonl` first — that file is the durable record `fha
    index` re-ingests into `search_log` on every full rebuild (which drops and
    recreates that table from scratch), so a capture survives a reindex even
    though the table itself doesn't persist it. When `.cache/index.sqlite`
    already exists, the row is *also* written straight into its `search_log`
    table (`person_id`/`source_id` are null — a stub has no resolved person
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


def _read_html(asset: Path | None) -> str:
    """Read page HTML from stdin, falling back to an HTML `--asset` file.

    The paste-fallback path pipes the page on stdin (`… | fha capture`); when no
    stdin is piped, an `--asset` that is itself the saved page is read as the
    HTML (BUILD.md M7.5: "Read HTML from stdin or `--asset`"). A binary asset
    (an image download) yields no usable HTML — the generic recipe then works
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
    dry_run: bool = False,
) -> int:
    """Capture a page into an inbox source stub and log the search (TOOLING §13b)."""
    recipes = _load_site_recipes()
    recipe_name, result = choose_recipe(html, url, recipes)

    # Explicit flags always win over recipe/generic inference (§13b: the human's
    # nudge beats the scrape). --type is validated against the controlled
    # vocabulary so a typo surfaces here, not as an unprocessable stub later.
    if title:
        result.title = title
    if source_type:
        st = source_type.strip().lower()
        if st not in SOURCE_TYPES:
            raise CaptureError(
                f'unknown source type {source_type!r}; valid types: '
                f'{", ".join(sorted(SOURCE_TYPES))}.'
            )
        result.source_type = st
    elif result.source_type not in SOURCE_TYPES:
        # A recipe must still stay within vocabulary; guard the generic default
        # already does, but a misbehaving recipe shouldn't write a bad stub.
        result.source_type = _GENERIC_SOURCE_TYPE
    if source_date:
        if not is_valid_edtf(source_date):
            raise CaptureError(f'--date must be EDTF (got {source_date!r}).')
        result.source_date = source_date
    elif result.source_date and not is_valid_edtf(str(result.source_date)):
        print(f'WARNING: recipe produced non-EDTF source_date {result.source_date!r}; '
              'dropping it from the stub.', file=sys.stderr)
        result.source_date = None

    # An explicit --url that no recipe surfaced still belongs in external_links.
    if url and not any((isinstance(l, dict) and l.get('url') == url) for l in result.external_links):
        result.external_links.insert(0, {'url': url})

    accessed = _today()
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
        return EXIT_CLEAN

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
    return EXIT_CLEAN


def _rel(path: Path, archive_root: Path) -> str:
    """Display a path relative to the archive root when possible, else posix."""
    try:
        return path.resolve().relative_to(archive_root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


# ── CLI ───────────────────────────────────────────────────────────────────────

def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'capture',
        help='Capture an open web record page into an inbox source stub',
        description=__doc__,
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
                   help="Override the source's own date (EDTF), e.g. 1880")
    p.add_argument('--asset', metavar='FILE',
                   help='Asset to stage alongside the stub (an image, or the saved page HTML)')
    p.add_argument('--dry-run', action='store_true', help='Preview without writing')


def _run_capture(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

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
        )
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
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    args = parser.parse_args(argv)
    return _run_capture(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
