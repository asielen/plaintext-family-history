#!/usr/bin/env python3
"""
fha serve - the localhost workbench (plan 17, Wave 3).

A human-started, foreground, 127.0.0.1-only web front door onto a family
archive. It serves the linked (unredacted) `fha site` build over HTTP and adds
an editing layer whose every button is a documented `fha` command, run
in-process and echoed after each apply (the parity rule made visible).

Guardrails restated (plan 17):
  - Parity: every button = one `fha` command; the CLI equivalent is echoed.
  - Front door: serve owns no state; `fha site` never depends on it; kill it and
    the archive is unchanged. The ONLY thing serve writes outside the record
    tree it mutates through the engines is its disposable snapshot under
    `.cache/serve/` (SPEC 5.6 disposability).
  - Not a daemon: foreground, 127.0.0.1 only, no auth, no network, no watching.
  - Human gate: every write is dry-run-preview -> explicit confirm, through the
    Result engines. The server defaults dry_run to true unless a POST explicitly
    says {"dry_run": false} (defense in depth behind the JS two-step).
  - Mechanical/generative boundary: serve never reads evidence to draft anything.
    Stage B stays with the AI skills.

Import exception (the tools-never-import-tools rule): serve.py is a FRONT DOOR
like fha.py, NOT an engine module. It is sanctioned to import the tool engines
(person, claim, source, confirm, process, capture, find, index, cooccur, xref)
and drive their run_* functions in-process. The rule that binds engine modules
does not bind dispatchers. site.py is loaded by path under a private name
(`fha_site`) exactly as fha.py does, because its stem shadows stdlib `site`.

Security is the whole trust boundary (there is no auth by design). See
`run_serve_preflight` and the `_Handler` methods; the invariants are:
  1. bind 127.0.0.1 only;  2. Host-header allowlist on every request;
  3. per-process CSRF token compared with hmac.compare_digest on every POST;
  4. canonical-path confinement (resolve + is_relative_to) on snapshot files,
     /root/ aliases, /api/open, /api/upload, and the `process.file` /api/run
     arg (`capture.path` is deliberately UNconfined - see _verb_capture_path);
  5. no-store + nosniff headers;  6. one global mutation lock held across
     mutate -> reindex -> snapshot-invalidate;  7. no state outside .cache/serve/.

Code map:
  _load_site_module         - import tools/site.py under a private name
  run_serve_preflight       - engine: archive/jinja/index/port checks -> Result
  ServeState                - shared per-process state (config, token, lock, dirs)
  _memoized / _MEMO_TTL     - generic short-TTL cache for the three render-time
                              memos below, guarded by state._memo_lock (NOT the
                              mutation lock - see _memoized's docstring)
  snapshot_is_stale / ensure_snapshot / invalidate_snapshot - refresh-on-use
  _cached_review_count / _cached_inbox_count / _counts - memoized queue counts
  _workbench_context        - the bar/CSRF/counts baked into every page
  gather_review / gather_inbox - the two serve-rendered pages' data
  VERBS + _verb_*           - the /api/run parity table (the one mutation door)
  _reindex_after            - post-write reindex policy (upsert vs full)
  _ThreadTee                - per-thread stdout/stderr router (process.py's
                              prints, captured without racing concurrent GETs)
  _Handler                  - the http.server request handler (routes + security)
  _cmd_serve / register     - the serving loop and CLI wiring
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import io
import json
import mimetypes
import os
import secrets
import shutil
import socket
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (  # noqa: E402
    ASSET_ROOT_ALIASES,
    EXIT_CLEAN,
    EXIT_FAILURE,
    FhaConfigError,
    Result,
    load_site_module,
    fmt_id_display,
    id_type_of,
    is_valid_id,
    is_working_copy,
    load_fha_yaml,
    normalize_id,
    open_index_db,
    read_text_exact,
    reapply_newline,
    resolve_path,
    resolve_root_arg,
    write_text_exact,
    yaml_inline,
)

# The engines serve drives in-process. Front-door imports (see module docstring).
import capture  # noqa: E402
import claim  # noqa: E402
import confirm  # noqa: E402
import cooccur  # noqa: E402
import find as find_mod  # noqa: E402
import index as index_mod  # noqa: E402
import person  # noqa: E402
import process as process_mod  # noqa: E402
import source as source_mod  # noqa: E402
import xref as xref_mod  # noqa: E402

try:
    import jinja2
except ImportError:  # pragma: no cover - preflight reports this plainly
    jinja2 = None

DEFAULT_PORT = 8765
_ASSET_ALIASES = ASSET_ROOT_ALIASES   # one shared constant with site.py's href writer
_MAX_UPLOAD_BYTES = 1 * 1024 * 1024 * 1024   # 1 GiB
_TEMPLATES_DIR = Path(__file__).parent / 'templates'
_STREAM_CHUNK_SIZE = 64 * 1024   # 64 KiB - amortizes syscall overhead without
                                 # holding a large per-request buffer in RAM.


def _load_site_module():
    """Import tools/site.py under a private module name.

    Thin wrapper over the one canonical loader (`_lib.load_site_module`, which
    documents the stdlib `site` name-collision quirk); fha.py delegates to the
    same helper, so the workaround has exactly one home."""
    return load_site_module()


# ── Preflight ────────────────────────────────────────────────────────────────

def run_serve_preflight(archive_root: Path, *, port: int = DEFAULT_PORT) -> Result:
    """Check everything serve needs before binding a socket; return a Result.

    Testable engine half of `fha serve` (the serving loop is in `_cmd_serve`).
    `data` carries {'status', 'archive_root', 'port', 'fha_config',
    'index_built'}. Refusals name the fix (AGENTS.md next-step rule): missing
    jinja2, a busy port (EADDRINUSE), an unreadable config, working-copy mode.
    A missing/stale index is not a refusal - it is rebuilt here and reported
    in `index_built`, so the first page render is never blocked on it."""
    result = Result(data={'status': None, 'archive_root': str(archive_root),
                          'port': port, 'fha_config': None, 'index_built': False})
    if jinja2 is None:
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = 'no-jinja'
        result.add('error',
                   'fha serve needs Jinja2 to render pages. Install it with '
                   '`python -m pip install jinja2`, then run `fha serve` again.',
                   next_step='python -m pip install jinja2')
        return result

    if is_working_copy(archive_root):
        # `fha site`'s own build treats working-copy mode as a clean, ok=True
        # warning (TOOLING §13d - the photo/document files are assumed
        # present elsewhere, not missing). `ensure_snapshot` only ever checks
        # `result.ok`, though, so if serve got this far it would stamp the
        # `.built` marker over a snapshot that was never actually written
        # (run_site returns zero pages here) - every workbench page then
        # 404s against an empty snapshot dir until the process is restarted.
        # Refusing here, before a socket is even bound, means the human
        # never reaches that broken, hard-to-diagnose state.
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = 'working-copy'
        result.add('error',
                   'fha serve is not available in working-copy mode - the photo '
                   'and document files are on the main machine, so the workbench '
                   'snapshot can never build here. Run `fha serve` on the main '
                   'machine instead.')
        return result

    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = 'bad-config'
        result.add('error', str(e))
        return result
    result.data['fha_config'] = fha_config

    # Rebuild the index when absent/stale so the first render and the review page
    # have a query surface. open_index_db with strict=True returns None on a
    # missing or stale db.
    conn = open_index_db(archive_root, ('persons', 'sources', 'claims'), strict=True)
    if conn is None:
        build = index_mod.build_index(archive_root, fha_config)
        result.data['index_built'] = True
        if not build.ok:
            result.ok = False
            result.exit_code = EXIT_FAILURE
            result.data['status'] = 'index-failed'
            result.add('error',
                       'could not build the search index. Run `fha index` and read '
                       'its message, then try `fha serve` again.',
                       next_step='fha index')
            return result
    else:
        conn.close()

    # Is the port bindable? Probe on 127.0.0.1 only (never 0.0.0.0). Deliberately
    # WITHOUT SO_REUSEADDR: http.server's ThreadingHTTPServer binds with
    # allow_reuse_address=True, and on Windows a second SO_REUSEADDR socket can
    # bind a port another SO_REUSEADDR socket already holds - so a probe that set
    # it would fail to notice a serve window already listening. Port 0 means "any
    # free port" (tests), which always binds.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(('127.0.0.1', port))
    except OSError:
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = 'port-busy'
        result.add('error',
                   f'port {port} is busy - close the other serve window or pass '
                   f'`--port {port + 1}`.',
                   next_step=f'fha serve --port {port + 1}')
        return result
    finally:
        probe.close()

    result.data['status'] = 'ok'
    result.add('info', f'serve preflight ok on 127.0.0.1:{port}')
    return result


# ── Shared per-process state ───────────────────────────────────────────────────

class ServeState:
    """Everything one serve process shares across request threads.

    The single `lock` serializes every mutation: a write, its reindex, and the
    snapshot invalidation all happen while one thread holds it, and the snapshot
    rebuild takes the same lock, so a rebuild can never interleave with a write
    (contract SS5.6).

    `_memo_lock` is a SEPARATE, smaller lock guarding three short-TTL caches
    read on every page GET: the record-tree staleness probe (`_mtime_memo`)
    and the review/inbox queue counts (`_review_count_memo`,
    `_inbox_count_memo`) - see `_memoized`'s docstring for why these are not
    just folded into `lock`. Each memo slot is `(monotonic_ts, value) | None`."""

    def __init__(self, archive_root: Path, fha_config: dict, port: int):
        self.archive_root = archive_root
        self.fha_config = fha_config
        self.port = port
        self.csrf_token = secrets.token_hex(16)
        self.lock = threading.Lock()
        self.site_mod = _load_site_module()
        self.snapshot_dir = archive_root / '.cache' / 'serve' / 'site'
        self.marker = self.snapshot_dir / '.built'
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=jinja2.select_autoescape(['html']),
        )
        self._memo_lock = threading.Lock()
        self._mtime_memo: tuple[float, float] | None = None
        self._review_count_memo: tuple[float, int] | None = None
        self._inbox_count_memo: tuple[float, int] | None = None

    @property
    def site_title(self) -> str:
        site_cfg = self.fha_config.get('site') if isinstance(self.fha_config.get('site'), dict) else {}
        return (str(site_cfg.get('archive_name')
                    or self.fha_config.get('archive_name') or '').strip()
                or 'Family History Archive')


# ── Snapshot: refresh-on-use, no watcher ───────────────────────────────────────

# Record trees whose mtimes decide snapshot staleness. Never the photos/documents
# roots (those can be huge and never change the rendered HTML structure enough to
# matter for a private preview).
_SNAPSHOT_INPUTS = ('sources', 'people', 'places', 'notes')


def _newest_mtime_under(base: Path) -> float:
    """Newest mtime of every file/dir under `base` (0.0 if `base` is absent).

    A directory's OWN mtime does not change when a file inside it is edited
    (only when an entry is added/removed/renamed) - so a single `os.stat` on
    a folder misses an in-place edit to a file it contains. Walking the small
    tree and taking the max over every file (and dir, to still catch a rename)
    is the only cheap way to answer "did anything in here change." Shared by
    the record-tree walk and the design/ walk in `_newest_input_mtime`."""
    newest = 0.0
    if not base.exists():
        return newest
    for dirpath, _dirs, files in os.walk(base):
        try:
            newest = max(newest, os.stat(dirpath).st_mtime)
        except OSError:
            pass
        for f in files:
            try:
                newest = max(newest, os.stat(os.path.join(dirpath, f)).st_mtime)
            except OSError:
                pass
    return newest


def _newest_input_mtime(state: ServeState) -> float:
    """Newest mtime across the record trees + design/ + fha.yaml + the index +
    the photo catalog. A cheap scandir walk of these small trees only
    (contract SS2) - the photos/documents ASSET roots themselves are never
    walked (could be huge; irrelevant to page HTML structure). `.cache/
    photos.sqlite` is a single stat, not a walk - the workbench renders photo
    strips, portraits, and captions straight from it, so a `fha photoindex
    scan`/`tag-person`/`set-summary` run while serve stays open must mark the
    snapshot stale exactly like an edited record does; without it those pages
    would keep serving the pre-update catalog until an unrelated watched file
    happened to change. `design/` is walked file-by-file like the record
    trees (see `_newest_mtime_under`), not single-`os.stat`'d on the
    directory: editing `design/custom.css` must mark the snapshot stale, and a
    directory-level stat would miss that edit."""
    newest = 0.0
    root = state.archive_root
    for name in _SNAPSHOT_INPUTS:
        newest = max(newest, _newest_mtime_under(root / name))
    newest = max(newest, _newest_mtime_under(root / 'design'))
    for extra in (root / 'fha.yaml', root / '.cache' / 'index.sqlite',
                  root / '.cache' / 'photos.sqlite'):
        try:
            newest = max(newest, os.stat(extra).st_mtime)
        except OSError:
            pass
    return newest


# TTL for the small render-time memos below (staleness probe + both queue
# counts). A page GET can land seconds after a hand-edit made outside serve
# (a text editor save, a git checkout) - the memo trades that gap for
# collapsing every request within the window onto one real recompute. 1.0s
# was picked to sit comfortably under a human's click-to-click cadence: a
# reload right after your own edit still shows it (that path also goes
# through `invalidate_snapshot`, which drops the memo immediately and does
# not wait out the TTL at all - see below); the TTL only covers an edit made
# by something OTHER than this serve session.
_MEMO_TTL = 1.0


def _memoized(state: ServeState, attr: str, compute) -> object:
    """Generic TTL memo shared by the staleness probe and the review/inbox
    counts (`_newest_input_mtime_cached`, `_cached_review_count`,
    `_cached_inbox_count`). `attr` names one of ServeState's `_*_memo` slots
    (each `(monotonic_ts, value) | None`); `compute` is called with no
    arguments on a cache miss.

    Why `state._memo_lock` and not `state.lock`: `state.lock` is held for an
    entire mutate -> reindex -> snapshot-invalidate sequence (an engine call,
    a full index rebuild - potentially seconds). These memos are read on
    EVERY GET, including ones that never touch a mutation; routing them
    through `state.lock` would stall every concurrent page render behind
    whatever POST happens to be mid-write, for no benefit (nothing here
    conflicts with a write - the memo is read-mostly derived data, not the
    write path itself). `_memo_lock` is only ever held for the length of one
    dict/tuple read or one write-back, NEVER around `compute()` - so the
    worst a race at the exact TTL boundary can cost is a duplicate
    recompute (two threads both miss the cache and both walk/query), never a
    torn read of a half-written memo tuple."""
    with state._memo_lock:
        cached = getattr(state, attr)
        now = time.monotonic()
        if cached is not None and (now - cached[0]) < _MEMO_TTL:
            return cached[1]
    value = compute()
    with state._memo_lock:
        setattr(state, attr, (time.monotonic(), value))
    return value


def _newest_input_mtime_cached(state: ServeState) -> float:
    """Memoized `_newest_input_mtime` (see `_memoized`/`_MEMO_TTL`).

    Without this, `snapshot_is_stale` walks the full record tree on EVERY
    page GET, and TWICE on a stale hit (`ensure_snapshot` calls it once
    before taking the lock, then again just inside the lock for the
    double-check). The memo collapses that to at most one real walk per TTL
    window, shared by every thread and both call sites."""
    return _memoized(state, '_mtime_memo', lambda: _newest_input_mtime(state))


def snapshot_is_stale(state: ServeState) -> bool:
    """True when the snapshot is missing, older than the newest record input,
    or built by a DIFFERENT serve process. The last check matters because the
    per-process CSRF token is baked into the snapshot's pages: a restarted
    serve mints a new token, so a snapshot left by the previous session would
    403 every Apply until something else invalidated it. The marker records
    which session built it; a marker from another session is always stale."""
    if not state.marker.exists():
        return True
    try:
        built = state.marker.stat().st_mtime
        marker_session = state.marker.read_text(encoding='utf-8').splitlines()[0].strip()
    except (OSError, IndexError):
        return True
    if marker_session != state.csrf_token:
        return True
    return _newest_input_mtime_cached(state) > built


def invalidate_snapshot(state: ServeState) -> None:
    """Delete the build marker so the next page GET rebuilds, and drop every
    render-time memo (the staleness probe + both queue counts). Caller holds
    the lock.

    Dropping the memos here (rather than letting the TTL expire on its own)
    matters for a write SERVE ITSELF just made: the record is already on disk
    by the time this runs, and the human who just clicked Apply expects the
    very next page load to reflect it - not up to `_MEMO_TTL` seconds of
    staleness on top of a change they just watched happen."""
    try:
        state.marker.unlink()
    except FileNotFoundError:
        pass
    with state._memo_lock:
        state._mtime_memo = None
        state._review_count_memo = None
        state._inbox_count_memo = None


def ensure_snapshot(state: ServeState) -> Result | None:
    """Rebuild the workbench snapshot if stale, holding the global lock so no
    write interleaves the rebuild. Idempotent and cheap when fresh (a scandir
    staleness probe, then return `None` - the common case, so most callers
    need not thread anything through their hot path).

    Returns the `run_site` `Result` whenever a rebuild was actually
    attempted, so a caller CAN tell success from a refusal/zero-pages
    outcome and refuse the request instead of silently serving whatever
    snapshot files are still sitting on disk from an earlier, now-stale
    build (P2 codex finding, round 6, PR #30 - a genuine `run_site` failure,
    e.g. `fha.yaml` edited into a bad state mid-session, used to just fall
    through here with no signal at all).

    `run_serve_preflight` already refuses to start `fha serve` at all in
    working-copy mode - but the archive can transition into that mode
    DURING a long-running serve session (a `WORKING_COPY` marker dropped in
    while the process is up), and `run_site` reports that case as `ok=True`
    with zero pages written (TOOLING §13d: a clean warning, not a failure,
    for `fha site`'s own CLI). Checking `result.data['status']` here too -
    not just `result.ok` - means the `.built` marker is never stamped over a
    snapshot that was never actually written; without this, every workbench
    page would 404 against an empty snapshot dir until the process restarts.
    Callers apply the same `status != 'working-copy'` check to the returned
    Result before trusting it to serve, for the identical reason."""
    if not snapshot_is_stale(state):
        return None
    with state.lock:
        if not snapshot_is_stale(state):
            return None   # another thread rebuilt while we waited
        # A record edited outside serve (a text editor save, a git checkout)
        # is exactly what makes `snapshot_is_stale` true in the first place -
        # but `run_site` below only WARNS on a stale `.cache/index.sqlite`
        # and still renders from its old rows (strict=False: a read-only
        # caller gets a slightly stale answer rather than none). Without
        # this, the rebuilt snapshot would be stamped fresh while quietly
        # showing pre-edit data, and `_counts` just below would undercount
        # the review/inbox queues the same way. Mirrors the same
        # open-or-rebuild check `run_serve_preflight` already does at
        # startup, just re-run here since the archive can go stale again at
        # any point during a long-running session.
        conn = open_index_db(state.archive_root, ('persons', 'sources', 'claims'), strict=True)
        if conn is None:
            index_mod.build_index(state.archive_root, state.fha_config)
        else:
            conn.close()
        review_count, inbox_count = _counts(state)
        ctx = {
            'port': state.port,
            'csrf_token': state.csrf_token,
            'review_count': review_count,
            'inbox_count': inbox_count,
        }
        result = state.site_mod.run_site(
            state.archive_root, state.snapshot_dir,
            linked=True, workbench=True, workbench_context=ctx,
        )
        if result.ok and result.data.get('status') != 'working-copy':
            state.snapshot_dir.mkdir(parents=True, exist_ok=True)
            # First line: the building session's token (see snapshot_is_stale);
            # second: a human-readable build time for anyone poking at .cache.
            state.marker.write_text(f'{state.csrf_token}\n{time.time()}\n', encoding='utf-8')
        return result


def _snapshot_failure_message(rebuild: Result) -> str:
    """Plain refusal text for a failed/zero-pages snapshot rebuild - the
    first error message `run_site` recorded, or a generic fallback naming
    where to look."""
    for m in rebuild.messages:
        if m.level == 'error':
            return m.text
    return 'the workbench snapshot could not be rebuilt - check the terminal fha serve runs in.'


def _snapshot_rebuild_failed(rebuild: Result | None) -> bool:
    """True when `ensure_snapshot` attempted a rebuild that did NOT leave a
    snapshot safe to serve as fresh - `None` (nothing needed rebuilding,
    already fresh) is not a failure. Shared by every `do_GET` route that
    calls `ensure_snapshot` before reading `.cache/serve/site/`, so a
    genuine `run_site` refusal (or a working-copy transition mid-session)
    refuses the request instead of silently falling through to whatever
    snapshot files an earlier, now-stale build left on disk."""
    return rebuild is not None and (not rebuild.ok or rebuild.data.get('status') == 'working-copy')


# ── Page context + counts ───────────────────────────────────────────────────────

def _cached_review_count(state: ServeState) -> int:
    """Memoized review-queue count (see `_memoized`). `gather_review` runs a
    full index query plus xref/cooccur detection - expensive enough that
    paying for it on every GET (the servebar count) on top of a page that
    already gathered the same items (the /review route itself) was 2-3x the
    real cost of one page load. A caller that already has the items in hand
    should pass its own count straight into `_workbench_context` instead of
    coming through here - see that function's `review_count` parameter."""
    def compute() -> int:
        try:
            return len(gather_review(state)['items'])
        except Exception:
            return 0
    return _memoized(state, '_review_count_memo', compute)


def _cached_inbox_count(state: ServeState) -> int:
    """Memoized inbox count - the `gather_inbox` counterpart of
    `_cached_review_count` (see its docstring)."""
    def compute() -> int:
        try:
            return len(gather_inbox(state)['items'])
        except Exception:
            return 0
    return _memoized(state, '_inbox_count_memo', compute)


def _counts(state: ServeState) -> tuple[int, int]:
    """(review_count, inbox_count) for the bar - each memoized independently
    so a caller that already has one half in hand (see `_workbench_context`)
    can skip just that half's recompute rather than pay for both again."""
    return _cached_review_count(state), _cached_inbox_count(state)


def _workbench_context(state: ServeState, *, review_count: int | None = None,
                       inbox_count: int | None = None) -> dict:
    """The bar/CSRF/counts baked into every rendered page.

    `review_count`/`inbox_count` let a caller that already gathered one
    queue's items this request (the /review and /inbox routes themselves,
    which need the full item list anyway to render their own content) pass
    that count straight through instead of triggering a second
    gather_review/gather_inbox via the cache - only the OTHER, not-already-
    in-hand half still goes through `_cached_review_count`/`_cached_inbox_count`."""
    if review_count is None:
        review_count = _cached_review_count(state)
    if inbox_count is None:
        inbox_count = _cached_inbox_count(state)
    return {
        'workbench': True,
        'csrf_token': state.csrf_token,
        'port': state.port,
        'review_count': review_count,
        'inbox_count': inbox_count,
        'site_title': state.site_title,
        'footer_note': '',
        'root_prefix': '.',
    }


# ── Review page data ─────────────────────────────────────────────────────────────

def gather_review(state: ServeState) -> dict:
    """Freshly query the review queue: suggested claims, xref candidates, and
    co-occurrence candidates (contract SS9). Reuses the same engines `fha report`
    reads - xref.run_xref and cooccur.run_cooccur - so detection is never
    reinvented. Returns {'items': [...], 'sources': [...], 'persons': [...]} where
    each item is a self-contained dict the template renders and whose action
    buttons carry the /api/run verb + fixed args."""
    root = state.archive_root
    items: list[dict] = []
    sources: dict[str, str] = {}
    persons: dict[str, str] = {}

    conn = open_index_db(root, ('persons', 'sources', 'claims'), strict=False)
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT c.id, c.type, c.value, c.date_edtf, c.place_text, c.confidence, "
                "c.source_id, s.title AS source_title "
                "FROM claims c LEFT JOIN sources s ON c.source_id = s.id "
                "WHERE c.status = 'suggested' ORDER BY c.source_id, c.id"
            ).fetchall()
            for r in rows:
                cid = r['id']
                people = [
                    pr['person_id'] for pr in conn.execute(
                        'SELECT person_id FROM claim_persons WHERE claim_id = ?', (cid,))
                ]
                pnames = []
                for pid in people:
                    nm = conn.execute('SELECT name FROM persons WHERE id = ?', (pid,)).fetchone()
                    label = (nm['name'] if nm and nm['name'] else fmt_id_display(pid))
                    pnames.append(label)
                    persons[pid] = label
                sid = r['source_id']
                stitle = r['source_title'] or (fmt_id_display(sid) if sid else 'no source')
                if sid:
                    sources[sid] = stitle
                items.append({
                    'kind': 'suggested claim',
                    'group_source': sid or 'unsourced',
                    'group_source_title': stitle,
                    'group_persons': people[:1],
                    'claim_id': fmt_id_display(cid),
                    'headline': r['value'] or f'{r["type"]} claim',
                    'meta': ' - '.join(x for x in (
                        r['type'],
                        f'date {r["date_edtf"]}' if r['date_edtf'] else None,
                        r['place_text'] or None,
                        ', '.join(pnames) if pnames else None,
                        f'confidence {r["confidence"]}' if r['confidence'] else None,
                    ) if x),
                    'person_labels': pnames,
                    'actions': 'claim',
                })
        except Exception:
            pass
        finally:
            conn.close()

    # Corroboration / contradiction candidates (xref).
    try:
        xr = xref_mod.run_xref(root)
        if xr.get('status') == 'ok':
            for grp in xr.get('groups', []):
                pid = grp.get('person_id')
                pname = grp.get('person_name') or (fmt_id_display(pid) if pid else '')
                if pid:
                    persons[pid] = pname
                for pair in grp.get('pairs', []):
                    ca = pair['claim_a']
                    cb = pair['claim_b']
                    kind = pair.get('kind', 'corroboration')
                    ca_src = ca.get('source_id')
                    if ca_src:
                        # Register the source so the review page has a slot to
                        # render this item into (its group_source below).
                        sources.setdefault(ca_src, ca.get('source_title') or fmt_id_display(ca_src))
                    items.append({
                        'kind': kind,
                        'group_source': ca_src or 'unsourced',
                        'group_source_title': ca.get('source_title') or '',
                        'group_persons': [pid] if pid else [],
                        'claim_a': fmt_id_display(ca['id']),
                        'claim_b': fmt_id_display(cb['id']),
                        'headline': 'Do these two claims relate?',
                        'pair_a': f'{ca.get("value") or ca.get("type")} ({fmt_id_display(ca["id"])})',
                        'pair_b': f'{cb.get("value") or cb.get("type")} ({fmt_id_display(cb["id"])})',
                        'meta': f'proposed by fha xref - {kind} candidate for {pname}',
                        'actions': 'xref',
                    })
    except Exception:
        pass

    # Co-occurrence candidates.
    try:
        co = cooccur.run_cooccur(root, threshold=2)
        if co.get('status') == 'ok':
            for c in co.get('person_pairs', [])[:25]:
                pa, pb = c['person_a'], c['person_b']
                persons[pa] = c.get('name_a') or fmt_id_display(pa)
                persons[pb] = c.get('name_b') or fmt_id_display(pb)
                src_ids = c.get('source_ids') or []
                src_id = src_ids[0] if src_ids else None
                if src_id:
                    sources.setdefault(src_id, fmt_id_display(src_id))
                items.append({
                    'kind': 'co-occurrence',
                    'group_source': src_id or 'unsourced',
                    'group_source_title': sources.get(src_id, '') if src_id else '',
                    'group_persons': [pa],
                    'person_a': fmt_id_display(pa),
                    'person_b': fmt_id_display(pb),
                    'source_id': fmt_id_display(src_id) if src_id else '',
                    'headline': f'{persons[pa]} & {persons[pb]} appear together',
                    'meta': f'{c.get("source_count", 0)} source(s)',
                    'actions': 'cooccur',
                })
    except Exception:
        pass

    return {
        'items': items,
        'sources': [{'id': sid, 'title': t} for sid, t in sources.items()],
        'persons': [{'id': pid, 'name': n} for pid, n in persons.items()],
    }


# ── Inbox page data ─────────────────────────────────────────────────────────────

def gather_inbox(state: ServeState) -> dict:
    """Group top-level inbox entries (contract SS10): an asset + its `.notes.md`
    sidecar is one item, a bundle folder is one item, a lone stub is one item.
    Returns {'items': [...]}, each with the paths for the open-file and
    file-as-a-source buttons.

    Pairing uses `fha process`'s own rule (`_find_sidecar`/`_companion_for_sidecar`
    in process.py, SPEC §12.1): a sidecar `{stem}.notes.md` pairs with the ONE
    other file whose stem equals `stem` exactly - never a prefix/startswith
    match, which would wrongly pair `photo.notes.md` with `photo.raw.jpg` too
    (both start with `photo.`, only one has stem `photo`). Precomputed into
    `companion_of_sidecar`/`sidecar_of_companion` before the display pass below
    so the pairing is independent of iteration order (the old code scanned a
    `set`, whose iteration order is not the display order, and so could pick a
    different "companion" from one run to the next). When more than one file
    shares the sidecar's exact stem, that is the same ambiguity
    `_companion_for_sidecar` refuses on - list the sidecar on its own rather
    than guess which asset it belongs to."""
    try:
        inbox = resolve_path('inbox', state.fha_config, state.archive_root)
    except Exception:
        return {'items': []}
    if not inbox.is_dir():
        return {'items': []}

    entries = sorted(inbox.iterdir(), key=lambda p: p.name.lower())
    files_only = [p for p in entries if p.is_file() and not p.name.startswith('.')]
    sidecar_files = [p for p in files_only if p.name.endswith('.notes.md')]
    asset_files = [p for p in files_only if not p.name.endswith('.notes.md')]

    companion_of_sidecar: dict[str, str] = {}
    sidecar_of_companion: dict[str, str] = {}
    for s in sidecar_files:
        base = s.name[:-len('.notes.md')]
        candidates = sorted(a.name for a in asset_files if a.stem == base)
        if len(candidates) == 1:
            companion_of_sidecar[s.name] = candidates[0]
            sidecar_of_companion[candidates[0]] = s.name

    items: list[dict] = []
    consumed: set[str] = set()
    for p in entries:
        if p.name in consumed or p.name.startswith('.'):
            continue
        rel = _inbox_rel(p, inbox)
        if p.is_dir():
            files = [f.name for f in sorted(p.iterdir()) if f.is_file()][:8]
            items.append({
                'name': p.name + '/', 'kind': 'bundle',
                'files': files, 'open_path': rel,
                'process_path': rel, 'sidecar': None,
            })
            consumed.add(p.name)
            continue
        if p.name.endswith('.notes.md'):
            companion = companion_of_sidecar.get(p.name)
            if companion is not None:
                consumed.add(companion)
                consumed.add(p.name)
                items.append({
                    'name': companion, 'kind': 'asset+note',
                    'files': [companion, p.name], 'open_path': _inbox_rel(inbox / companion, inbox),
                    'process_path': _inbox_rel(inbox / companion, inbox),
                    'sidecar': _inbox_rel(p, inbox),
                })
            else:
                consumed.add(p.name)
                items.append({
                    'name': p.name, 'kind': 'note',
                    'files': [p.name], 'open_path': rel, 'process_path': rel, 'sidecar': None,
                })
            continue
        # A bare asset: look for its sidecar (the precomputed exact-stem match).
        sidecar = sidecar_of_companion.get(p.name)
        if sidecar is not None:
            consumed.add(sidecar)
            items.append({
                'name': p.name, 'kind': 'asset+note',
                'files': [p.name, sidecar], 'open_path': rel, 'process_path': rel,
                'sidecar': _inbox_rel(inbox / sidecar, inbox),
            })
        else:
            items.append({
                'name': p.name, 'kind': 'asset',
                'files': [p.name], 'open_path': rel, 'process_path': rel, 'sidecar': None,
            })
        consumed.add(p.name)

    return {'items': items}


def _inbox_rel(p: Path, inbox: Path) -> str:
    """Forward-slash inbox-relative path (`inbox/foo.md`) for process/open args."""
    try:
        return 'inbox/' + p.relative_to(inbox).as_posix()
    except ValueError:
        return 'inbox/' + p.name


# ── /api/run - the parity table (whitelist) ─────────────────────────────────────

def _q(value: str) -> str:
    """Double-quote a CLI echo argument the way a POSIX shell needs it: the
    banner's "this button is exactly" command is meant to be copy-pasted and
    re-run verbatim, so it must be safe even when the value holds shell
    metacharacters. Inside double quotes only backslash, `"`, `$` and `` ` ``
    are special (`&`, `;`, `(`, spaces, etc. are already literal there), so
    escaping those four and always quoting - rather than only when a space or
    quote is present - closes both the unquoted-`&`-style operator case and
    the `$(...)`/backtick command-substitution case."""
    s = str(value)
    escaped = s.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('`', '\\`')
    return '"' + escaped + '"'


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(',') if v.strip()]


def _coerce(schema: dict, args: dict) -> tuple[dict, str | None]:
    """Validate `args` against a verb's schema (whitelist). Unknown keys are a
    plain refusal (the /api/run 400). Returns (clean_kwargs, error)."""
    extra = [k for k in args if k not in schema]
    if extra:
        return {}, ('unexpected field(s): ' + ', '.join(sorted(extra))
                    + '. Allowed: ' + ', '.join(sorted(schema)) + '.')
    out: dict = {}
    for name, kind in schema.items():
        if name not in args:
            continue
        v = args[name]
        if kind == 'bool':
            out[name] = (v is True or str(v).strip().lower() in ('true', '1', 'yes', 'on'))
        elif kind == 'list':
            out[name] = _as_list(v)
        else:
            out[name] = None if v is None else str(v)
    return out, None


def _verb_claim_review(state, kw, dry_run):
    return claim.run_claim(
        state.archive_root, claim_id=kw.get('claim_id', ''),
        status=kw.get('status'), value=kw.get('value'), date=kw.get('date'),
        claim_type=kw.get('claim_type'), place=kw.get('place'),
        place_text=kw.get('place_text'), persons=kw.get('persons'),
        confidence=kw.get('confidence'), dry_run=dry_run)


def _echo_claim_review(kw):
    parts = ['fha claim', kw.get('claim_id', '?')]
    if kw.get('status'):
        parts += ['--status', kw['status']]
    if kw.get('value'):
        parts += ['--value', _q(kw['value'])]
    if kw.get('date'):
        parts += ['--date', _q(kw['date'])]
    if kw.get('claim_type'):
        parts += ['--type', kw['claim_type']]
    if kw.get('place'):
        parts += ['--place', kw['place']]
    if kw.get('place_text'):
        parts += ['--place-text', _q(kw['place_text'])]
    if kw.get('persons'):
        parts += ['--persons', ','.join(kw['persons'])]
    if kw.get('confidence'):
        parts += ['--confidence', kw['confidence']]
    return ' '.join(parts)


def _verb_claim_new(state, kw, dry_run):
    return claim.run_claim_new(
        state.archive_root, source_id=kw.get('source_id', ''),
        claim_type=kw.get('claim_type', ''), value=kw.get('value', ''),
        date=kw.get('date'), place=kw.get('place'), place_text=kw.get('place_text'),
        persons=kw.get('persons'), subtype=kw.get('subtype'),
        status=kw.get('status') or 'accepted', confidence=kw.get('confidence'),
        dry_run=dry_run,
        # Threaded back in by the workbench's Apply step from the id its own
        # earlier dry-run preview minted and showed the human, so Apply
        # commits exactly that claim instead of `run_claim_new` minting a
        # second, different C-id (P2 codex finding, round 5, PR #30). The
        # CLI (`fha claim new`) never sends this - the schema key exists
        # only for this verb's own round-trip.
        claim_id=kw.get('claim_id'))


def _echo_claim_new(kw):
    parts = ['fha claim new', '--source', kw.get('source_id', '?'),
             '--type', kw.get('claim_type', '?'), '--value', _q(kw.get('value', ''))]
    if kw.get('date'):
        parts += ['--date', _q(kw['date'])]
    if kw.get('place'):
        parts += ['--place', kw['place']]
    if kw.get('place_text'):
        parts += ['--place-text', _q(kw['place_text'])]
    if kw.get('persons'):
        parts += ['--persons', ','.join(kw['persons'])]
    if kw.get('subtype'):
        parts += ['--subtype', kw['subtype']]
    if kw.get('status'):
        parts += ['--status', kw['status']]
    if kw.get('confidence'):
        parts += ['--confidence', kw['confidence']]
    return ' '.join(parts)


def _verb_xref(state, kw, dry_run):
    return confirm.run_confirm_xref(
        state.archive_root, claim_a=kw.get('claim_a', ''), claim_b=kw.get('claim_b', ''),
        relation=kw.get('relation', ''), dry_run=dry_run)


def _echo_xref(kw):
    return (f'fha confirm xref {kw.get("claim_a", "?")} {kw.get("claim_b", "?")} '
            f'--as {kw.get("relation", "?")}')


def _verb_cooccur(state, kw, dry_run):
    return confirm.run_confirm_cooccur(
        state.archive_root, person_a=kw.get('person_a', ''), person_b=kw.get('person_b', ''),
        source_id=kw.get('source_id', ''), subtype=kw.get('subtype', ''),
        accept=bool(kw.get('accept', True)), dry_run=dry_run)


def _echo_cooccur(kw):
    parts = ['fha confirm cooccur', kw.get('person_a', '?'), kw.get('person_b', '?'),
             '--source', kw.get('source_id', '?'), '--subtype', kw.get('subtype', '?')]
    if kw.get('accept', True):
        parts.append('--accept')
    return ' '.join(parts)


def _verb_dismiss(state, kw, dry_run):
    return confirm.run_dismiss(
        state.archive_root, person_a=kw.get('person_a', ''),
        person_b=kw.get('person_b', ''), dry_run=dry_run)


def _echo_dismiss(kw):
    return f'fha confirm dismiss {kw.get("person_a", "?")} {kw.get("person_b", "?")}'


def _verb_set_living(state, kw, dry_run):
    return person.run_set_living(
        state.archive_root, kw.get('person_id', ''), kw.get('value', ''), dry_run=dry_run)


def _echo_set_living(kw):
    return f'fha person set-living {kw.get("person_id", "?")} {kw.get("value", "?")}'


def _verb_person_new(state, kw, dry_run):
    return person.run_new(
        state.archive_root, kw.get('name', ''), sex=kw.get('sex'), gender=kw.get('gender'),
        birth=kw.get('birth'), death=kw.get('death'), dry_run=dry_run,
        # Threaded back in by the workbench's Apply step from the id its own
        # earlier dry-run preview minted and showed the human, so Apply
        # commits exactly that person instead of `run_new` minting a second,
        # different P-id (P2 codex finding, round 5, PR #30). The CLI
        # (`fha person new`) never sends this - the schema key exists only
        # for this verb's own round-trip.
        person_id=kw.get('person_id'))


def _echo_person_new(kw):
    parts = ['fha person new', _q(kw.get('name', ''))]
    for flag in ('sex', 'gender', 'birth', 'death'):
        if kw.get(flag):
            parts += [f'--{flag}', _q(kw[flag])]
    return ' '.join(parts)


def _verb_relate(state, kw, dry_run):
    return person.run_relate(
        state.archive_root, kw.get('person_id', ''), kw.get('relation_type', ''),
        kw.get('target_id', ''), subtype=kw.get('subtype'),
        reciprocal=bool(kw.get('reciprocal', False)), dry_run=dry_run)


def _echo_relate(kw):
    parts = ['fha person relate', kw.get('person_id', '?'),
             f'--{kw.get("relation_type", "spouse")}', kw.get('target_id', '?')]
    if kw.get('subtype'):
        parts += ['--subtype', kw['subtype']]
    if kw.get('reciprocal'):
        parts.append('--reciprocal')
    return ' '.join(parts)


def _verb_estimate(state, kw, dry_run):
    return person.run_estimate(
        state.archive_root, kw.get('person_id', ''),
        birth=kw.get('birth'), death=kw.get('death'), dry_run=dry_run)


def _echo_estimate(kw):
    parts = ['fha person estimate', kw.get('person_id', '?')]
    if kw.get('birth'):
        parts += ['--birth', _q(kw['birth'])]
    if kw.get('death'):
        parts += ['--death', _q(kw['death'])]
    return ' '.join(parts)


def _verb_person_edit(state, kw, dry_run):
    return person.run_edit(
        state.archive_root, kw.get('person_id', ''), kw.get('section', ''),
        text=kw.get('text'), append=bool(kw.get('append', False)), dry_run=dry_run)


def _echo_person_edit(kw):
    parts = ['fha person edit', kw.get('person_id', '?'), '--section', kw.get('section', '?'),
             '--text', _q(kw.get('text', ''))]
    if kw.get('append'):
        parts.append('--append')
    return ' '.join(parts)


def _verb_person_note(state, kw, dry_run):
    return person.run_note(
        state.archive_root, kw.get('person_id', ''), kw.get('section', ''),
        kw.get('text', ''), dry_run=dry_run)


def _echo_person_note(kw):
    return (f'fha person note {kw.get("person_id", "?")} --section '
            f'{kw.get("section", "?")} --text {_q(kw.get("text", ""))}')


def _verb_source_note(state, kw, dry_run):
    return source_mod.run_source_note(
        state.archive_root, kw.get('source_id', ''), text=kw.get('text', ''), dry_run=dry_run)


def _echo_source_note(kw):
    return f'fha source note {kw.get("source_id", "?")} --text {_q(kw.get("text", ""))}'


# ── Per-thread stdout/stderr routing (process.py's own prints) ────────────────

class _ThreadTee(io.TextIOBase):
    """A stdout/stderr stand-in that routes each write by the CALLING thread.

    `fha serve` runs every HTTP request on its own thread (ThreadingHTTPServer).
    `_verb_process` drives `process.py`'s `run_process`, which still legitimately
    prints (refactoring it to the Result contract is its own project - out of
    scope here) - that output has to be folded into THIS request's Result
    without swallowing, or being swallowed by, whatever any OTHER thread prints
    at the same moment (a concurrent GET rebuilding the snapshot, a traceback
    logged by the request handler). `contextlib.redirect_stdout` swaps
    `sys.stdout` for the whole PROCESS, so two concurrent `process.file` calls
    would race on the same buffer - each could capture (or lose) lines the
    other thread printed. A `threading.local` override fixes that: a write
    goes to the CALLING thread's buffer if one was installed with
    `set_buffer`, else straight through to the real stream underneath - so an
    unrelated thread with no buffer set is completely unaffected.

    Installed ONCE, over `sys.stdout`/`sys.stderr`, at server startup
    (`_cmd_serve`, before `serve_forever`) and restored in `finally` on exit."""

    def __init__(self, real: io.TextIOBase) -> None:
        super().__init__()
        self._real = real
        self._local = threading.local()

    def set_buffer(self, buf: io.StringIO | None) -> None:
        """Install (`buf`) or clear (`None`) the CALLING thread's capture buffer."""
        self._local.buf = buf

    def write(self, s: str) -> int:
        buf = getattr(self._local, 'buf', None)
        return (buf if buf is not None else self._real).write(s)

    def flush(self) -> None:
        buf = getattr(self._local, 'buf', None)
        (buf if buf is not None else self._real).flush()

    @property
    def encoding(self) -> str:  # some libraries probe this before writing
        return getattr(self._real, 'encoding', 'utf-8')


def _classify_captured_line(line: str) -> str:
    """process.py's own message-level convention: an `ERROR:`/`WARNING:` prefix
    on a printed line IS its severity (there is no structured level to read
    since these are plain prints, not a Result); anything else is 'info'."""
    if line.startswith('ERROR'):
        return 'error'
    if line.startswith('WARNING'):
        return 'warning'
    return 'info'


def _verb_process(state, kw, dry_run):
    """Stage A filing. Confine the path, then drive run_process with an explicit
    --type so it never prompts. stdin is swapped to EOF so an unexpected prompt
    fails cleanly (a plain message) instead of hanging a request thread - it is
    scoped to this one call because nothing else in serve ever reads stdin.
    stdout/stderr are captured through THIS thread's `_ThreadTee` buffer
    (installed once at server startup, see `_cmd_serve`) rather than a
    process-global `contextlib.redirect_stdout` - a concurrent GET on another
    thread can never write into, or lose output to, this request's buffer. If
    no tee is installed (serve not started through `_cmd_serve`, e.g. a test
    driving this function directly) the buffer simply captures nothing and the
    generic 'filed'/'preview complete' message is used instead - a graceful
    fallback, not a silent bug, since process.py's own exit code still governs
    the Result either way."""
    raw = kw.get('file', '')
    confined, err = _confine_asset_path(state, raw)
    if err:
        return Result(ok=False, exit_code=EXIT_FAILURE).add('error', err)
    ns = argparse.Namespace(
        file=str(confined), source_type=kw.get('source_type'), title=kw.get('title'),
        slug=kw.get('slug'), source_date=None, more=None, people=None,
        dry_run=dry_run, root=str(state.archive_root),
        # Threaded back in by the workbench's Apply step from the id its own
        # earlier dry-run preview minted and showed the human, so Apply
        # commits exactly that source instead of `_mint_one_source_id`
        # minting a second, different S-id (P2 codex finding, round 7, PR
        # #30 - the same preview/apply mismatch already fixed for
        # person.new/claim.new). Not a real `fha process` CLI flag - this
        # Namespace attribute exists only for this verb's own round-trip.
        source_id=kw.get('source_id'),
    )
    buf = io.StringIO()
    out_tee = sys.stdout if isinstance(sys.stdout, _ThreadTee) else None
    err_tee = sys.stderr if isinstance(sys.stderr, _ThreadTee) else None
    if out_tee is not None:
        out_tee.set_buffer(buf)
    if err_tee is not None:
        err_tee.set_buffer(buf)
    old_stdin = sys.stdin
    sys.stdin = io.StringIO('')
    try:
        res = process_mod.run_process(ns)
    except EOFError:
        return Result(ok=False, exit_code=EXIT_FAILURE).add(
            'error', 'this item needs the command line (it has variations or a bundle '
                     'that must be chosen interactively). Run it in a terminal.',
            next_step=f'fha process {_q(raw)} --type {kw.get("source_type", "other")}')
    except Exception as e:  # noqa: BLE001 - a filing failure becomes a plain message
        return Result(ok=False, exit_code=EXIT_FAILURE).add('error', f'could not file: {e}')
    finally:
        sys.stdin = old_stdin
        if out_tee is not None:
            out_tee.set_buffer(None)
        if err_tee is not None:
            err_tee.set_buffer(None)
    out = Result(ok=res.ok, exit_code=res.exit_code, data=dict(res.data or {}))
    for line in buf.getvalue().splitlines():
        if line.strip():
            out.add(_classify_captured_line(line), line)
    if not out.messages:
        out.add('info', 'filed' if not dry_run else 'preview complete')
    return out


def _echo_process(kw):
    parts = ['fha process', _q(kw.get('file', ''))]
    if kw.get('source_type'):
        parts += ['--type', kw['source_type']]
    if kw.get('title'):
        parts += ['--title', _q(kw['title'])]
    if kw.get('slug'):
        parts += ['--slug', kw['slug']]
    return ' '.join(parts)


def _verb_capture_path(state, kw, dry_run):
    """Register a must-never-move asset via `fha capture --path` (TOOLING §13b).

    Deliberately has NO `_confine_asset_path` gate (unlike `_verb_process`):
    pointing at a file OUTSIDE the archive tree is the whole point of this
    verb - a photo still living in someone else's library, registered without
    ever being moved, renamed, or opened. The engine only ever
    `.exists()`-checks the target path; it never reads or writes it. CSRF
    already gates the POST, so confinement would protect nothing here that
    isn't already protected - the write this verb performs is the pointer
    stub in inbox/, which `run_capture_path` itself confines.

    A RELATIVE path is resolved against `state.archive_root` for the ENGINE'S
    existence check only (`run_capture_path`'s `check_path`): a bare relative
    path resolved against this PROCESS's own current directory - the
    server's launch directory, not the browser's - would be meaningless for
    a value a human typed into a form. The raw, as-typed value is still what
    gets passed as `path` (and so stored as the record's `asset_path`,
    TOOLING §13b) - resolving it to an archive-root-absolute string before
    the engine ever saw it used to overwrite that field with a machine-
    specific path the human never typed (P2 codex finding, round 6, PR #30).
    An absolute path (the common case, since the point of `--path` is a file
    living elsewhere) needs no `check_path` override - `path` alone already
    resolves the same way from any cwd.

    Reads `Result.messages` straight from the engine with no redirect: since
    `run_capture_path` follows the house engine contract (returns a Result,
    never prints), there is nothing here to capture."""
    raw = kw.get('path', '')
    check_path = None
    if raw and not Path(raw).is_absolute():
        check_path = state.archive_root / raw
    try:
        return capture.run_capture_path(
            state.archive_root, state.fha_config, path=raw, check_path=check_path,
            note=kw.get('note'), title=kw.get('title'), dry_run=dry_run)
    except Exception as e:  # noqa: BLE001
        return Result(ok=False, exit_code=EXIT_FAILURE).add('error', f'could not register: {e}')


def _echo_capture_path(kw):
    parts = ['fha capture', '--path', _q(kw.get('path', ''))]
    if kw.get('note'):
        parts += ['--note', _q(kw['note'])]
    if kw.get('title'):
        parts += ['--title', _q(kw['title'])]
    return ' '.join(parts)


def _verb_publish(state, kw, dry_run):
    out_dir = state.archive_root / 'generated' / 'site'
    return state.site_mod.run_site(state.archive_root, out_dir, linked=False, dry_run=dry_run)


def _echo_publish(kw):
    return 'fha site --standalone'


def _verb_index(state, kw, dry_run):
    if dry_run:
        return Result(ok=True, exit_code=EXIT_CLEAN).add(
            'info', 'would rebuild the search index (.cache/index.sqlite).')
    return index_mod.build_index(state.archive_root, state.fha_config)


def _echo_index(kw):
    return 'fha index'


def _verb_home_edit(state, kw, dry_run):
    """Bounded write of notes/home.md - parity with editing the file directly.
    Dry-run shows a unified diff (contract SS6).

    Reads/writes through `read_text_exact`/`write_text_exact` and reapplies the
    file's own newline convention with `reapply_newline` (the same byte-faithful
    pattern the surgical claim/profile editors use) - a plain `Path.read_text`/
    `write_text` round-trip would silently convert every line of a CRLF-authored
    notes/home.md to LF, churning the whole file instead of just the human's
    edit. A brand-new file has no existing convention to match, so it gets the
    plain '\\n' the caller already builds `new` with."""
    import difflib
    text = kw.get('text')
    if text is None or not str(text).strip():
        return Result(ok=False, exit_code=EXIT_FAILURE).add(
            'error', 'the homepage intro was empty - nothing to write.')
    path = state.archive_root / 'notes' / 'home.md'
    new = str(text)
    if not new.endswith('\n'):
        new += '\n'
    old = read_text_exact(path) if path.exists() else ''
    new = reapply_newline(new, old)
    if old == new:
        return Result(ok=True, exit_code=EXIT_CLEAN).add('info', 'no change - notes/home.md already matches.')
    result = Result(data={'status': 'dry-run' if dry_run else 'ok'})
    diff = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile='notes/home.md (before)', tofile='notes/home.md (after)', lineterm=''))
    for line in diff[:60]:
        result.add('info', line)
    if dry_run:
        return result
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_exact(path, new)
    except OSError as e:
        return Result(ok=False, exit_code=EXIT_FAILURE).add('error', f'could not write notes/home.md: {e}')
    result.note_changed(path)
    result.add('info', 'notes/home.md updated.')
    return result


def _echo_home_edit(kw):
    return '# notes/home.md is yours - same as editing it in any text editor'


# key -> (schema, run, echo, reindex-policy). reindex: 'source' (upsert the
# source named in Result.data, else full), 'full', or 'none'.
VERBS: dict[str, dict] = {
    'claim.review': {'schema': {'claim_id': 'str', 'status': 'str', 'value': 'str',
                               'date': 'str', 'claim_type': 'str', 'place': 'str',
                               'place_text': 'str', 'persons': 'list', 'confidence': 'str'},
                     'run': _verb_claim_review, 'echo': _echo_claim_review, 'reindex': 'source'},
    'claim.new': {'schema': {'source_id': 'str', 'claim_type': 'str', 'value': 'str',
                            'date': 'str', 'place': 'str', 'place_text': 'str',
                            'persons': 'list', 'subtype': 'str', 'status': 'str',
                            'confidence': 'str', 'claim_id': 'str'},
                  'run': _verb_claim_new, 'echo': _echo_claim_new, 'reindex': 'source'},
    'confirm.xref': {'schema': {'claim_a': 'str', 'claim_b': 'str', 'relation': 'str'},
                     'run': _verb_xref, 'echo': _echo_xref, 'reindex': 'full'},
    'confirm.cooccur': {'schema': {'person_a': 'str', 'person_b': 'str', 'source_id': 'str',
                                  'subtype': 'str', 'accept': 'bool'},
                        'run': _verb_cooccur, 'echo': _echo_cooccur, 'reindex': 'source'},
    'confirm.dismiss': {'schema': {'person_a': 'str', 'person_b': 'str'},
                        'run': _verb_dismiss, 'echo': _echo_dismiss, 'reindex': 'none'},
    'person.set-living': {'schema': {'person_id': 'str', 'value': 'str'},
                          'run': _verb_set_living, 'echo': _echo_set_living, 'reindex': 'full'},
    'person.new': {'schema': {'name': 'str', 'sex': 'str', 'gender': 'str',
                             'birth': 'str', 'death': 'str', 'person_id': 'str'},
                   'run': _verb_person_new, 'echo': _echo_person_new, 'reindex': 'full'},
    'person.relate': {'schema': {'person_id': 'str', 'relation_type': 'str', 'target_id': 'str',
                                'subtype': 'str', 'reciprocal': 'bool'},
                      'run': _verb_relate, 'echo': _echo_relate, 'reindex': 'full'},
    'person.estimate': {'schema': {'person_id': 'str', 'birth': 'str', 'death': 'str'},
                        'run': _verb_estimate, 'echo': _echo_estimate, 'reindex': 'full'},
    'person.edit': {'schema': {'person_id': 'str', 'section': 'str', 'text': 'str', 'append': 'bool'},
                    'run': _verb_person_edit, 'echo': _echo_person_edit, 'reindex': 'full'},
    'person.note': {'schema': {'person_id': 'str', 'section': 'str', 'text': 'str'},
                    'run': _verb_person_note, 'echo': _echo_person_note, 'reindex': 'full'},
    'source.note': {'schema': {'source_id': 'str', 'text': 'str'},
                    'run': _verb_source_note, 'echo': _echo_source_note, 'reindex': 'source'},
    'process.file': {'schema': {'file': 'str', 'source_type': 'str', 'title': 'str', 'slug': 'str',
                                'source_id': 'str'},
                     'run': _verb_process, 'echo': _echo_process, 'reindex': 'full'},
    'capture.path': {'schema': {'path': 'str', 'note': 'str', 'title': 'str'},
                     'run': _verb_capture_path, 'echo': _echo_capture_path, 'reindex': 'full'},
    'site.publish': {'schema': {}, 'run': _verb_publish, 'echo': _echo_publish, 'reindex': 'none'},
    'index.rebuild': {'schema': {}, 'run': _verb_index, 'echo': _echo_index, 'reindex': 'none'},
    'home.edit': {'schema': {'text': 'str'}, 'run': _verb_home_edit, 'echo': _echo_home_edit,
                  'reindex': 'full'},
}


def _reindex_after(state: ServeState, verb: str, result: Result) -> None:
    """Post-write reindex policy (contract SS6). Caller holds the lock.
    A source-scoped verb upserts the one source it named; else a full rebuild.
    publish/index.rebuild need no extra reindex.

    Reads ONLY `data['source_id']` - the canonical `S-…` id `run_claim` and
    `run_confirm_cooccur` publish specifically for this reindexer to read.
    (An earlier version also guessed at `data['source']`, but that key holds
    the source record's file PATH, not an id - it never validated as an ID
    and always fell through to a full rebuild anyway, so checking it bought
    nothing but confusion. Keep this single-key read the day any other
    'source'-scoped verb is added.)"""
    policy = VERBS[verb]['reindex']
    if policy == 'none':
        return
    if policy == 'source':
        sid = result.get('source_id')
        if sid and is_valid_id(sid) and id_type_of(normalize_id(sid)) == 'S':
            outcome = index_mod.upsert_source(state.archive_root, state.fha_config, normalize_id(sid))
            if outcome == 'indexed':
                return
        # Fall through to a full rebuild when we could not target a source.
    index_mod.build_index(state.archive_root, state.fha_config)


def run_api_run(state: ServeState, verb: str, args: dict, dry_run: bool) -> tuple[int, dict]:
    """Execute one /api/run request. Returns (http_status, payload). The whole
    mutate -> reindex -> invalidate sequence is under the global lock so a
    rebuild can never interleave a write."""
    spec = VERBS.get(verb)
    if spec is None:
        return 400, {'ok': False, 'exit_code': EXIT_FAILURE,
                     'messages': [{'level': 'error',
                                   'text': f'unknown verb {verb!r}. This build allows: '
                                           + ', '.join(sorted(VERBS)) + '.',
                                   'next_step': None}],
                     'changed': [], 'data': {}, 'cli_echo': ''}
    kw, err = _coerce(spec['schema'], args)
    if err:
        return 400, {'ok': False, 'exit_code': EXIT_FAILURE,
                     'messages': [{'level': 'error', 'text': err, 'next_step': None}],
                     'changed': [], 'data': {}, 'cli_echo': ''}

    with state.lock:
        try:
            result = spec['run'](state, kw, dry_run)
        except Exception as e:  # noqa: BLE001 - never leak a traceback to the browser
            traceback.print_exc()
            return 500, {'ok': False, 'exit_code': EXIT_FAILURE,
                         'messages': [{'level': 'error',
                                       'text': f'the {verb} engine failed: {e}',
                                       'next_step': None}],
                         'changed': [], 'data': {}, 'cli_echo': spec['echo'](kw)}
        if not dry_run and result.ok:
            try:
                _reindex_after(state, verb, result)
                if verb != 'site.publish':
                    invalidate_snapshot(state)
            except Exception as e:  # noqa: BLE001 - the engine write already
                # landed on disk by this point; letting this escape (like the
                # engine-call except above does) would answer 500/'internal
                # error' and the do_POST wrapper's generic message, so the
                # browser reports "Nothing was written" for a change that
                # already happened - inviting a human to retry it. Surface
                # this as a warning on the still-successful result instead:
                # `changed` below already lists what the engine wrote: only
                # the follow-up refresh failed, and the human needs to know
                # to run it by hand.
                traceback.print_exc()
                result.add(
                    'warning',
                    f'saved, but the search index/snapshot could not refresh '
                    f'automatically ({e}). Run `fha index`, or restart fha serve, '
                    f'to bring the view up to date.',
                    next_step='fha index')

    payload = result.as_dict()
    payload['cli_echo'] = spec['echo'](kw)
    return 200, payload


# ── Path confinement ─────────────────────────────────────────────────────────────

def _confine_asset_path(state: ServeState, raw: str, must_exist: bool = True
                        ) -> tuple[Path | None, str | None]:
    """Confine a path-shaped /api/run arg (`process.file`'s `file`) to the
    archive tree or an allowed asset root before the engine ever sees it
    (defense in depth). Returns (resolved, error).

    NOT used by `capture.path`: registering a file OUTSIDE the archive tree
    is that verb's whole purpose (see `_verb_capture_path`'s docstring), so
    confining it here would refuse the feature it exists to provide."""
    if not raw or '\x00' in raw:
        return None, 'no path was given.'
    root = state.archive_root.resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = _alias_prefixed_path(state, raw)
    try:
        resolved = candidate.resolve()
    except OSError as e:
        return None, f'that path could not be resolved ({e}).'
    bases = [root]
    for alias in _ASSET_ALIASES:
        try:
            bases.append(resolve_path(alias, state.fha_config, state.archive_root).resolve())
        except Exception:
            pass
    if not any(_within(resolved, b) for b in bases):
        return None, ('that path is outside the archive and its asset folders - '
                      'serve only files things from inside the archive.')
    if must_exist and not resolved.exists():
        return None, f'no file at {raw} (checked under the archive).'
    return resolved, None


def _within(path: Path, base: Path) -> bool:
    try:
        return path == base or path.is_relative_to(base)
    except (ValueError, OSError):
        return False


def _resolve_root_request(state: ServeState, alias: str, relpath: str) -> Path | None:
    """Resolve a GET /root/<alias>/<relpath> to a confined, existing FILE, or
    None. Only photos/documents/inbox aliases; canonical-path confinement to the
    alias's resolved base; no directory listings."""
    if alias not in _ASSET_ALIASES:
        return None
    if '\x00' in relpath or '\\' in relpath:
        return None
    try:
        base = resolve_path(alias, state.fha_config, state.archive_root).resolve()
    except Exception:
        return None
    candidate = (base / relpath)
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not _within(resolved, base):
        return None
    if not resolved.is_file():
        return None
    return resolved


def _resolve_snapshot_request(state: ServeState, url_path: str) -> Path | None:
    """Resolve a static GET to a file under the snapshot site dir, or None. `/`
    maps to index.html. Canonical-path confinement rejects .., encoded dots,
    backslashes, drive letters, NUL."""
    rel = unquote(url_path.lstrip('/'))
    if rel == '' or rel.endswith('/'):
        rel = rel + 'index.html'
    if '\x00' in rel or '\\' in rel:
        return None
    site_dir = state.snapshot_dir.resolve()
    candidate = site_dir / rel
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not _within(resolved, site_dir):
        return None
    if not resolved.is_file():
        return None
    return resolved


# ── Multipart upload parsing ───────────────────────────────────────────────────

def _parse_multipart(content_type: str, body: bytes) -> dict:
    """Minimal multipart/form-data parser (the cgi module is gone in 3.13+).
    Returns {'file': (filename, bytes) | None, 'text': {name: value}}."""
    out = {'file': None, 'text': {}}
    marker = 'boundary='
    idx = content_type.find(marker)
    if idx < 0:
        return out
    boundary = content_type[idx + len(marker):].strip().strip('"')
    if not boundary:
        return out
    delim = ('--' + boundary).encode('latin-1')
    parts = body.split(delim)
    for part in parts:
        if part in (b'', b'--', b'--\r\n', b'\r\n'):
            continue
        part = part.lstrip(b'\r\n')
        if part.startswith(b'--'):
            continue
        header_blob, _, content = part.partition(b'\r\n\r\n')
        if not _:
            continue
        content = content[:-2] if content.endswith(b'\r\n') else content
        headers = header_blob.decode('latin-1', 'replace')
        name = _header_param(headers, 'name')
        filename = _header_param(headers, 'filename')
        if name is None:
            continue
        if filename is not None:
            out['file'] = (filename, content)
        else:
            out['text'][name] = content.decode('utf-8', 'replace')
    return out


def _header_param(headers: str, key: str) -> str | None:
    token = key + '="'
    i = headers.find(token)
    if i < 0:
        return None
    i += len(token)
    j = headers.find('"', i)
    return headers[i:j] if j > i - 1 else None


# Win32 reserved device names: writing to `CON.part` (any extension) hits the
# console device on classic Windows semantics, not a file. Self-inflicted at
# worst (uploads are CSRF-gated), but a plain refusal beats a hung write.
_WIN_RESERVED_NAMES = frozenset(
    {'CON', 'PRN', 'AUX', 'NUL'}
    | {f'COM{i}' for i in range(1, 10)} | {f'LPT{i}' for i in range(1, 10)}
)


def _sanitize_basename(name: str) -> str | None:
    """Basename only, path separators + NUL stripped; reject empty/dot names,
    Windows reserved device names, and trailing dots (Win32 silently strips
    them, which would make the name collide with its dotless sibling)."""
    if not name or '\x00' in name:
        return None
    base = os.path.basename(name.replace('\\', '/'))
    base = base.strip().rstrip('.')
    if base in ('', '.', '..'):
        return None
    if base.split('.')[0].upper() in _WIN_RESERVED_NAMES:
        return None
    return base


def run_api_upload(state: ServeState, filename: str, data: bytes,
                   what: str = '', who: str = '') -> tuple[int, dict]:
    """Write uploaded bytes into inbox/<basename> (collision -> ` -2` stem) plus
    an optional `.notes.md` sidecar. Basename-only, sanitized, confined to the
    resolved inbox root.

    The sidecar is named `<stem>.notes.md` (process.py's `_find_sidecar` rule,
    SPEC §12.1 - `photo.jpg` <-> `photo.notes.md`), never `<full name>.notes.md`,
    or `fha process` would never find it. Its content follows what
    `_read_sidecar` actually consumes: the "what" text becomes the PROSE BODY
    (that is what lands in the scaffolded record's `## Notes`), and the "who"
    hint is written under `people:` - the one frontmatter key `_read_sidecar`
    reads for unresolved names, which it folds into that same body as an
    "unreconciled" line. A sidecar written any other way would sit in the
    inbox looking filed but silently never feed the fields it looks like it
    should."""
    base = _sanitize_basename(filename)
    if base is None:
        return 400, _msg_payload(False, 'that file name is not allowed. Give a plain file name.')
    try:
        inbox = resolve_path('inbox', state.fha_config, state.archive_root).resolve()
    except Exception:
        return 500, _msg_payload(False, 'could not find the inbox folder.')

    # One line per field, collapsed before it goes anywhere near YAML or a
    # record body: a raw newline in a browser-typed note would otherwise
    # inject extra keys into the sidecar's frontmatter.
    what_line = ' '.join(what.split())
    who_line = ' '.join(who.split())
    has_note = bool(what_line or who_line)

    def _sidecar_name(dest_name: str) -> str:
        return os.path.splitext(dest_name)[0] + '.notes.md'

    with state.lock:
        inbox.mkdir(parents=True, exist_ok=True)
        dest = inbox / base
        stem, ext = os.path.splitext(base)
        n = 2
        # A note's sidecar shares the ASSET's stem, not its full name (same
        # rule as above) - so a pre-existing sidecar at the stem's name must
        # bump the destination exactly like a collision on the asset name
        # itself, regardless of whether THIS upload adds a note: `_find_sidecar`/
        # `gather_inbox` pair an asset with any same-stem sidecar it finds,
        # so filing a bare `foo.jpg` next to an unrelated pre-existing
        # `foo.notes.md` would silently attach that stranger's note to this
        # source. The write below lands at `dest.name + '.part'` first
        # (write-then-atomic-replace); a PRE-EXISTING file at that exact
        # `.part` name - a genuine partial download someone left in the
        # inbox - must bump the destination too, or the write below clobbers
        # it before the rename ever happens.
        while (dest.exists() or (inbox / _sidecar_name(dest.name)).exists()
               or (inbox / (dest.name + '.part')).exists()):
            dest = inbox / f'{stem} -{n}{ext}'
            n += 1
        # Confirm the final destination is still inside the inbox (belt + braces).
        if not _within(dest.resolve() if dest.exists() else dest, inbox):
            return 400, _msg_payload(False, 'refused - the destination escaped the inbox.')
        tmp = inbox / (dest.name + '.part')
        try:
            tmp.write_bytes(data)
            tmp.replace(dest)
        except OSError as e:
            with contextlib.suppress(OSError):
                tmp.unlink()
            return 500, _msg_payload(False, f'could not write the file: {e}')
        written = [str(dest)]
        sidecar_error: str | None = None
        if has_note:
            sidecar = inbox / _sidecar_name(dest.name)
            lines = ['---', f'noted: {time.strftime("%Y-%m-%d")}']
            if who_line:
                # A list, not a bare scalar: `_read_sidecar` reads `people:`
                # as `meta.get('people') or []` and iterates it - a plain
                # string value would iterate its characters, not its name(s).
                lines.append(f'people: {yaml_inline([who_line])}')
            lines.append('---')
            lines.append('')
            lines.append(what_line if what_line else '*(uploaded with a name hint - no note given)*')
            lines.append('')
            try:
                sidecar.write_text('\n'.join(lines), encoding='utf-8')
                written.append(str(sidecar))
            except OSError as e:
                # The asset itself is already safely saved (written above) -
                # only the note sidecar failed. Report it as a warning rather
                # than swallowing it: a silent 200 here would tell the human
                # their note was kept when it was actually lost.
                sidecar_error = str(e)
        snapshot_error: str | None = None
        try:
            invalidate_snapshot(state)
        except Exception as e:  # noqa: BLE001 - the file(s) already landed on
            # disk by this point; letting this escape (uncaught, like an
            # engine exception) would answer do_POST's generic 500/'internal
            # error', and the workbench renders that as "Upload refused" for
            # a file that is actually already safely in the inbox - inviting
            # a human to re-upload it and create a duplicate. Report it as a
            # warning on the still-successful response instead (same shape
            # as round 5's run_api_run reindex/snapshot-invalidation fix).
            traceback.print_exc()
            snapshot_error = str(e)

    if sidecar_error is not None:
        payload = _msg_payload(
            True,
            f'added inbox/{dest.name}, but the note could not be saved: '
            f'{sidecar_error}. The file itself is safe - add the note by hand '
            f'beside it (inbox/{_sidecar_name(dest.name)}), or re-upload once '
            'the problem is fixed.',
        )
        payload['messages'][0]['level'] = 'warning'
    else:
        payload = _msg_payload(True, f'added inbox/{dest.name}'
                               + (' with a note beside it' if len(written) > 1 else ''))
    if snapshot_error is not None:
        payload['messages'].append({
            'level': 'warning',
            'text': f'the file was saved, but the workbench view could not refresh '
                    f'automatically ({snapshot_error}). Reload the page, or restart '
                    'fha serve, to see it.',
            'next_step': None,
        })
    payload['changed'] = written
    return 200, payload


def _alias_prefixed_path(state: ServeState, raw: str) -> Path:
    """Resolve a RELATIVE path that may be alias-prefixed (`inbox/foo.jpg`,
    `documents/census/x.jpg` - the shape `gather_inbox`/`gather_review` hand
    to the open/process APIs) through that alias's CONFIGURED root, falling
    back to a plain archive-root join for anything else.

    `fha.yaml`'s `roots:` mapping may point photos/documents/inbox OUTSIDE
    the archive root (AGENTS_TOOLING's config-surface check) - `Path(raw)`
    joined straight onto `archive_root` silently produces the wrong location
    (and then a false "file not found") whenever the alias in question is
    remapped, because the string's `<alias>/` prefix was never resolved
    through `resolve_path`, only assumed to mean `archive_root/<alias>/…`."""
    for alias in _ASSET_ALIASES:
        prefix = alias + '/'
        if raw == alias or raw.startswith(prefix):
            try:
                base = resolve_path(alias, state.fha_config, state.archive_root)
            except Exception:
                break
            rest = raw[len(prefix):] if raw.startswith(prefix) else ''
            return (base / rest) if rest else base
    return state.archive_root / raw


def run_api_open(state: ServeState, path: str) -> tuple[int, dict]:
    """OS-open a record/asset file after confinement (contract SS8). The target
    must resolve under the archive root or an allowed asset root."""
    if not path or '\x00' in path:
        return 400, _msg_payload(False, 'no path was given.')
    root = state.archive_root.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = _alias_prefixed_path(state, path)
    try:
        resolved = candidate.resolve()
    except OSError as e:
        return 400, _msg_payload(False, f'that path could not be resolved ({e}).')
    bases = [root]
    for alias in _ASSET_ALIASES:
        try:
            bases.append(resolve_path(alias, state.fha_config, state.archive_root).resolve())
        except Exception:
            pass
    if not any(_within(resolved, b) for b in bases):
        return 403, _msg_payload(False, 'that file is outside the archive - serve will not open it.')
    if not resolved.exists():
        return 404, _msg_payload(False, f'no file at {path}.')
    try:
        _os_open(resolved)
    except Exception as e:  # noqa: BLE001
        return 500, _msg_payload(False, f'could not open the file ({e}). Open it from Explorer instead.')
    return 200, _msg_payload(True, f'opened {resolved.name} in your usual editor.')


def _os_open(path: Path) -> None:
    """Open a file with the OS default handler. Windows is the target
    (os.startfile); other platforms fall back to open/xdg-open."""
    if sys.platform.startswith('win'):
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606 - Windows default handler
    elif sys.platform == 'darwin':
        import subprocess
        subprocess.Popen(['open', str(path)])
    else:
        import subprocess
        subprocess.Popen(['xdg-open', str(path)])


def _msg_payload(ok: bool, text: str) -> dict:
    return {'ok': ok, 'exit_code': EXIT_CLEAN if ok else EXIT_FAILURE,
            'messages': [{'level': 'info' if ok else 'error', 'text': text, 'next_step': None}],
            'changed': [], 'data': {}}


# ── The HTTP handler ────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    server_version = 'fha-serve'
    protocol_version = 'HTTP/1.1'

    @property
    def state(self) -> ServeState:
        return self.server.state  # type: ignore[attr-defined]

    # -- security gates --

    def _host_ok(self) -> bool:
        """Host header hostname must be 127.0.0.1 or localhost (DNS-rebinding
        defense). The port part (if present) is whatever port actually reached
        this 127.0.0.1-bound socket, so the hostname is the real check: a page on
        evil.com that rebinds DNS to 127.0.0.1 still sends `Host: evil.com` and is
        refused here."""
        host = (self.headers.get('Host') or '').strip().lower()
        if not host:
            return False
        # Strip the optional :port (rsplit so an IPv6 literal would not be
        # mangled; only bracketless localhost/127.0.0.1 are allowed anyway).
        hostname = host.rsplit(':', 1)[0] if ':' in host and not host.endswith(':') else host
        hostname = hostname.strip('[]')
        return hostname in ('127.0.0.1', 'localhost')

    def _csrf_ok(self) -> bool:
        token = self.headers.get('X-FHA-CSRF') or ''
        # Compare as bytes: compare_digest on str raises TypeError for
        # non-ASCII, and a garbage header must be a plain 403, not an error.
        return hmac.compare_digest(token.encode('utf-8', 'replace'),
                                   self.state.csrf_token.encode('utf-8'))

    # -- response helpers --

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if self.close_connection:
            # A caller (currently only `_read_body`'s over-cap/unparseable
            # path) already decided this socket must not be reused for
            # keep-alive - setting the flag alone stops THIS server's loop
            # from reading another request off it (see BaseHTTPRequestHandler
            # .handle), but the client also needs the header to know not to
            # try. Sending it here, once, covers every 4xx/5xx this method
            # renders rather than repeating the header at each call site.
            self.send_header('Connection', 'close')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _send_json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode('utf-8'), 'application/json; charset=utf-8')

    def _send_text(self, code: int, text: str) -> None:
        self._send(code, text.encode('utf-8'), 'text/plain; charset=utf-8')

    def _reject(self, code: int, text: str) -> None:
        self._send_text(code, text)

    def _not_found(self) -> None:
        self._send_text(404, 'Not found.\n\nfha serve serves this archive at:\n'
                        '  /            the home page\n'
                        '  /persons/... /sources/... /places/...   record pages\n'
                        '  /review      the review queue\n'
                        '  /inbox       the inbox\n'
                        '  /root/{photos,documents,inbox}/...   asset files\n')

    # -- routing --

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._host_ok():
            self._reject(403, 'Refused: unexpected Host header (fha serve is 127.0.0.1 only).')
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        try:
            if path == '/api/find':
                self._handle_find(parse_qs(parsed.query))
                return
            if path.startswith('/root/'):
                self._handle_root_asset(path)
                return
            if path == '/review':
                rebuild = ensure_snapshot(self.state)
                if _snapshot_rebuild_failed(rebuild):
                    self._reject(503, f'fha serve could not refresh its view: '
                                      f'{_snapshot_failure_message(rebuild)}')
                    return
                # This gather IS the servebar's review count - thread it
                # through instead of letting _workbench_context trigger a
                # second gather_review via the cache (see _render_page).
                review_data = gather_review(self.state)
                self._render_page('review.html', review_data, title='Review',
                                  review_count=len(review_data['items']))
                return
            if path == '/inbox':
                rebuild = ensure_snapshot(self.state)
                if _snapshot_rebuild_failed(rebuild):
                    self._reject(503, f'fha serve could not refresh its view: '
                                      f'{_snapshot_failure_message(rebuild)}')
                    return
                inbox_data = gather_inbox(self.state)
                self._render_page('inbox.html', inbox_data, title='Inbox',
                                  inbox_count=len(inbox_data['items']))
                return
            if path.startswith('/api/'):
                self._not_found()
                return
            # Static snapshot file.
            rebuild = ensure_snapshot(self.state)
            if _snapshot_rebuild_failed(rebuild):
                self._reject(503, f'fha serve could not refresh its view: '
                                  f'{_snapshot_failure_message(rebuild)}')
                return
            resolved = _resolve_snapshot_request(self.state, path)
            if resolved is None:
                self._not_found()
                return
            self._send_file(resolved)
        except Exception:  # noqa: BLE001 - never leak a traceback to the browser
            traceback.print_exc()
            self._reject(500, 'fha serve hit an internal error - see the terminal it runs in.')

    def do_POST(self) -> None:
        if not self._host_ok():
            self._reject(403, 'Refused: unexpected Host header (fha serve is 127.0.0.1 only).')
            return
        if not self._csrf_ok():
            self._reject(403, 'Refused: missing or wrong security token. Restart the page you '
                              'opened from fha serve (reload it) and try again.')
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        try:
            if path == '/api/run':
                self._handle_run()
            elif path == '/api/upload':
                self._handle_upload()
            elif path == '/api/open':
                self._handle_open()
            elif path == '/api/reindex':
                self._handle_reindex()
            else:
                self._not_found()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._send_json(500, _msg_payload(False, 'fha serve hit an internal error - see the terminal.'))

    # -- handlers --

    def _read_body(self, cap: int = _MAX_UPLOAD_BYTES) -> bytes | None:
        """Read exactly Content-Length bytes, or None when the declared
        length is unusable (unparseable) or over `cap`.

        A `None` return is always followed by the caller sending a 4xx with
        NOTHING drained from the socket. On HTTP/1.1 keep-alive that is a
        desync bug: the next request `handle_one_request` reads off this same
        connection would start mid-body of the one just refused, and every
        request after that misreads too. Actually draining an over-cap body
        (which this cap exists precisely to avoid reading - up to a GiB) is
        worse than the fix: `self.close_connection = True` tells
        `BaseHTTPRequestHandler.handle`'s loop to close the socket after this
        response instead of waiting for another request on it, and `_send`
        mirrors that with a `Connection: close` header so the CLIENT also
        knows not to reuse it."""
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            self.close_connection = True
            return None
        if length < 0 or length > cap:
            self.close_connection = True
            return None
        return self.rfile.read(length) if length else b''

    def _handle_run(self) -> None:
        body = self._read_body(cap=32 * 1024 * 1024)
        if body is None:
            self._send_json(413, _msg_payload(False, 'request too large.'))
            return
        try:
            req = json.loads(body or b'{}')
        except json.JSONDecodeError:
            self._send_json(400, _msg_payload(False, 'the request body was not valid JSON.'))
            return
        verb = req.get('verb')
        args = req.get('args') or {}
        # Defense in depth: dry_run defaults to TRUE. Only an explicit false runs.
        dry_run = not (req.get('dry_run') is False)
        if not isinstance(args, dict):
            self._send_json(400, _msg_payload(False, '"args" must be an object.'))
            return
        code, payload = run_api_run(self.state, verb, args, dry_run)
        self._send_json(code, payload)

    def _handle_upload(self) -> None:
        ctype = self.headers.get('Content-Type') or ''
        if 'multipart/form-data' not in ctype:
            self._send_json(400, _msg_payload(False, 'upload must be multipart/form-data.'))
            return
        body = self._read_body()
        if body is None:
            self._send_json(413, _msg_payload(False, 'that file is too large (limit 1 GiB).'))
            return
        parsed = _parse_multipart(ctype, body)
        if not parsed['file']:
            self._send_json(400, _msg_payload(False, 'no file was in the upload.'))
            return
        filename, data = parsed['file']
        text = parsed['text']
        code, payload = run_api_upload(self.state, filename, data,
                                       what=text.get('what', ''), who=text.get('who', ''))
        self._send_json(code, payload)

    def _handle_open(self) -> None:
        body = self._read_body(cap=64 * 1024)
        try:
            req = json.loads(body or b'{}')
        except json.JSONDecodeError:
            self._send_json(400, _msg_payload(False, 'the request body was not valid JSON.'))
            return
        code, payload = run_api_open(self.state, req.get('path', ''))
        self._send_json(code, payload)

    def _handle_reindex(self) -> None:
        # This route reads no fields from a body - but unlike the other POST
        # handlers it never called `_read_body` at all, so any body the
        # caller sent (a stray Content-Length from a generic fetch()
        # wrapper) was left undrained on the socket, desyncing the next
        # request on the same keep-alive connection (see `_read_body`'s
        # docstring). A small bound is enough since none is ever expected.
        body = self._read_body(cap=64 * 1024)
        if body is None:
            self._send_json(413, _msg_payload(False, 'request too large.'))
            return
        with self.state.lock:
            index_mod.build_index(self.state.archive_root, self.state.fha_config)
            invalidate_snapshot(self.state)
        self._send_json(200, _msg_payload(True, 'index rebuilt; pages will refresh.'))

    def _handle_find(self, query: dict) -> None:
        q = (query.get('q', [''])[0] or '').strip()
        kind_raw = query.get('kind', [None])[0]
        try:
            limit = int(query.get('limit', ['20'])[0])
        except ValueError:
            limit = 20
        limit = max(1, min(limit, 50))
        kinds = [k.strip() for k in kind_raw.split(',')] if kind_raw else None
        results = find_mod.search_json(self.state.archive_root, self.state.fha_config,
                                       q, kinds=kinds, limit=limit)
        self._send_json(200, {'results': results})

    def _handle_root_asset(self, path: str) -> None:
        rest = unquote(path[len('/root/'):])
        alias, _, relpath = rest.partition('/')
        resolved = _resolve_root_request(self.state, alias, relpath)
        if resolved is None:
            self._reject(404, 'Not found or not allowed.')
            return
        self._send_file(resolved)

    def _send_file(self, path: Path) -> None:
        """Stream `path` to the client in `_STREAM_CHUNK_SIZE` chunks instead
        of reading it whole into memory first - a large scan/video used to be
        fully buffered in RAM (and off the socket) before a single byte went
        out. Content-Length still comes from a `stat()`, so the client gets
        an accurate length up front exactly as before; HEAD still sends
        headers only, no chunks. No Range/206 support this pass - a
        seek-based partial-content responder is its own feature, not a
        one-line addition to a chunked GET."""
        ctype, _ = mimetypes.guess_type(str(path))
        try:
            size = path.stat().st_size
            f = path.open('rb')
        except OSError:
            self._not_found()
            return
        try:
            self.send_response(200)
            self.send_header('Content-Type', ctype or 'application/octet-stream')
            self.send_header('Content-Length', str(size))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            if self.command != 'HEAD':
                try:
                    shutil.copyfileobj(f, self.wfile, _STREAM_CHUNK_SIZE)
                except (ConnectionAbortedError, BrokenPipeError):
                    # The client went away mid-download (closed tab,
                    # cancelled fetch) - not a server error, nothing to log
                    # or retry.
                    pass
        finally:
            f.close()

    def _render_page(self, template: str, data: dict, *, title: str,
                     review_count: int | None = None, inbox_count: int | None = None) -> None:
        ctx = _workbench_context(self.state, review_count=review_count, inbox_count=inbox_count)
        ctx.update(data)
        ctx['page_title'] = title
        try:
            tmpl = self.state.env.get_template(template)
            html_out = tmpl.render(**ctx)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._reject(500, f'could not render {template}: {e}')
            return
        self._send(200, html_out.encode('utf-8'), 'text/html; charset=utf-8')

    def log_message(self, fmt, *args):  # noqa: A003 - quiet the default stderr spam
        pass


# ── Serving loop + CLI ──────────────────────────────────────────────────────────

def _resolved_port(args: argparse.Namespace) -> int:
    """The bind port `_cmd_serve` uses: `--port`'s value, or DEFAULT_PORT when
    `--port` is genuinely ABSENT.

    `int(getattr(args, 'port', None) or DEFAULT_PORT)` looks equivalent but is
    not: `0 or DEFAULT_PORT` evaluates to `DEFAULT_PORT` because `0` is falsy,
    so an explicit `--port 0` (a legal bind request - "any free port," what the
    test suite passes) silently became 8765 and never reached preflight/bind.
    Only `None` (the argument truly not given) should fall back; any int the
    user typed, including 0, must pass through untouched."""
    port = getattr(args, 'port', None)
    return DEFAULT_PORT if port is None else int(port)


def _cmd_serve(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    port = _resolved_port(args)

    pre = run_serve_preflight(archive_root, port=port)
    for m in pre.messages:
        if m.level in ('error', 'warning'):
            prefix = 'ERROR' if m.level == 'error' else 'WARNING'
            print(f'{prefix}: {m.text}', file=sys.stderr)
    if not pre.ok:
        return pre.exit_code

    fha_config = pre.data['fha_config']

    # Bind BEFORE building state/snapshot: `--port 0` asks the OS for a free
    # port, and only the bound socket knows what it actually got. Binding
    # first (rather than after ensure_snapshot) keeps the snapshot's embedded
    # `port:` context - which workbench pages read to build their own API
    # URLs - and the printed/opened URL honest for the ephemeral-port path
    # the test suite relies on; building the snapshot against the requested
    # port (still 0) would bake a dead URL into every rendered page.
    try:
        httpd = ThreadingHTTPServer(('127.0.0.1', port), _Handler)
    except OSError:
        print(f'ERROR: port {port} is busy - close the other serve window or pass '
              f'`--port {port + 1}`.', file=sys.stderr)
        return EXIT_FAILURE
    port = httpd.server_address[1]

    state = ServeState(archive_root, fha_config, port)
    httpd.state = state  # type: ignore[attr-defined]

    # Build the first snapshot up front so the first page render is instant.
    ensure_snapshot(state)
    review_count, inbox_count = _counts(state)

    url = f'http://127.0.0.1:{port}/'
    print('')
    print(f'  {state.site_title}')
    print(f'  serving at  {url}')
    print('  this machine only - no network, no auth')
    print('  linked view (unredacted - private; sharing still goes through '
          '`fha site --standalone`)')
    print(f'  Review: {review_count}   Inbox: {inbox_count}')
    if pre.data.get('index_built'):
        print('  (rebuilt the search index first)')
    print('  stop: Ctrl-C (nothing is lost)')
    print('')
    sys.stdout.flush()   # block-buffered when piped; show the banner at once

    if not getattr(args, 'no_browser', False):
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - a headless box just skips this
            pass

    # Install the per-thread stdout/stderr router ONCE, for the life of the
    # serve loop (see _ThreadTee's docstring): every request runs on its own
    # thread, and _verb_process needs to capture process.py's prints into
    # THAT thread's Result without racing a concurrent GET on another thread.
    # Restored in `finally` so a crash or Ctrl-C never leaves the real
    # terminal streams wrapped.
    real_stdout, real_stderr = sys.stdout, sys.stderr
    sys.stdout = _ThreadTee(real_stdout)
    sys.stderr = _ThreadTee(real_stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nfha serve stopped. Nothing was lost.', file=sys.stderr)
    finally:
        httpd.server_close()
        sys.stdout, sys.stderr = real_stdout, real_stderr
    return EXIT_CLEAN


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = subs.add_parser(
        'serve',
        help='Open the localhost workbench - a private, editable view of the archive.',
        description=(
            'Start a local, private web workbench for this archive.\n\n'
            '  fha serve                 open it in your browser on 127.0.0.1:8765\n'
            '  fha serve --port 8766     use a different port\n'
            '  fha serve --no-browser    start it but do not open a browser\n\n'
            'It runs on this machine only - no network, no login. Every button is an '
            'fha command, previewed before it writes. Stop it with Ctrl-C; nothing is lost.'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--port', type=int, default=DEFAULT_PORT,
                   help=f'Port to listen on (default {DEFAULT_PORT}).')
    p.add_argument('--no-browser', action='store_true', dest='no_browser',
                   help='Do not open a web browser on startup.')
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p.set_defaults(func=_cmd_serve)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='fha serve')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--no-browser', action='store_true', dest='no_browser')
    parser.add_argument('--root', metavar='PATH')
    parser.set_defaults(func=_cmd_serve)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
