#!/usr/bin/env python3
"""
stubs.py - fha stubs: mint person stubs for unresolved P-id references.

  fha stubs                           Scan claims and create missing stubs
  fha stubs --from-names "A; B; C"    Mint new P-ids + stubs for named people
  fha stubs --dry-run                 Preview without writing

Creates {surname}__{given}_{P-id}.md in people/stubs/.
Never overwrites; never moves a stub out of stubs/ (placement is a human act).
TOOLING §5.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    archive_root_missing_message,
    find_archive_root,
    id_type_of,
    is_template_file,
    link_field_refs,
    load_fha_yaml,
    mint_ids,
    normalize_id,
    read_record,
    render_person_body_scaffold,
    render_stub_content,
    stub_filename,
    stub_slug_name,
    undecodable_file_recorder,
    write_text_exact_atomic,
)


def _rel(path: Path, archive_root: Path) -> str:
    """A path as the human filed it (archive-relative), never a local absolute one."""
    try:
        return path.relative_to(archive_root).as_posix()
    except ValueError:
        return str(path).replace('\\', '/')


# The slugging/filename/content rendering below now lives in `_lib.py`
# (`stub_slug_name` / `stub_filename` / `render_stub_content`) so `fha person
# new` can share it. These thin wrappers keep this module's private names
# (and every existing call site/test that imports `stubs._slug_name` etc.)
# working unchanged; note `_stub_filename` keeps ITS historical (pid, name)
# argument order even though the shared `stub_filename` takes (name, pid).
def _slug_name(name: str) -> tuple[str, str]:
    return stub_slug_name(name)


def _stub_filename(pid: str, name: str | None) -> str:
    return stub_filename(name, pid)


def _stub_content(pid: str, name: str | None) -> str:
    """Frontmatter plus the full #75/#76 body (purpose block, the not-yet-
    generated `## Sources` placeholder, and the four hand-written sections) -
    the same shape `fha person new` writes, so a stub reads identically no
    matter which tool minted it.

    The extra `'\\n'` between the two renderers is the blank-line separator
    `_TEMPLATE.person.md` (and every other scaffolded record) carries between
    the frontmatter's closing fence and the H1 - `render_stub_content` ends
    in only a single `'\\n'` (its own byte-identical contract; see
    `tests/test_stubs.py`), so without it the H1 would land on the very next
    line with no breathing room."""
    return render_stub_content(pid, name) + '\n' + render_person_body_scaffold(name or 'unknown')


def _collect_unresolved_persons(archive_root: Path) -> dict[str, str | None]:
    """
    Scan source claims for P-ids that have no person record.
    Returns {pid: name_guess | None}.

    Name guessing is intentionally minimal here: claim values have varied
    structure and reliable name extraction isn't worth the complexity.
    The biographer gives the stub a real name when they promote it from stubs/.
    # TODO: extract name from claim value when claim type is 'relationship'
    #   and the value follows the "{name} is a child of …" pattern - that
    #   would give us a name hint for most auto-generated relationship claims.

    This is a whole-archive scan, so a single file saved in another encoding
    (cp1252, a Windows editor's default) must not blind it to every other file
    (#68): each loop skips an undecodable file rather than letting
    `read_record`'s default `UnicodeDecodeError` take the whole command down,
    and both loops share one `undecodable` list (one `on_decode_error`
    callback, built once) so a file touched by both passes - unlikely here
    since people/ and sources/ do not overlap, but cheap to guarantee - earns
    one warning, not two. The warning is printed once at the end, after both
    loops, naming what a skip actually costs: a stub might get (re-)minted for
    someone who already has a record (the people/ loop never saw their id), or
    a real reference inside that file's claims goes unseen this run (the
    sources/ loop never read them) - matching `fha index`'s own aggregated
    undecodable-files warning in tone.
    """
    undecodable: list[Path] = []
    on_decode_error = undecodable_file_recorder(undecodable)

    # Collect all known P-ids from existing person files
    known_pids: set[str] = set()
    people_root = archive_root / 'people'
    if people_root.exists():
        for path in people_root.rglob('*.md'):
            if is_template_file(path):
                continue   # `_TEMPLATE.*` placeholder ids are not real records
            rec = read_record(path, on_decode_error=on_decode_error)
            if rec['undecodable']:
                continue
            pid = normalize_id(str(rec['meta'].get('id', '')))
            if pid and pid.startswith('p-'):
                known_pids.add(pid)

    # Scan source claims for P-ids not in known_pids. Entries go through
    # link_field_refs so a wrapped `[[P-…]]` / `[[P-…|Name]]` reference is seen
    # as its bare P-id - previously `str(p_raw)` kept the brackets, the
    # startswith('p-') test failed, and the exact refs lint E005 points at
    # ("create a stub with `fha stubs`") were silently skipped. Non-ID names
    # are still skipped here: a stub is only mintable for an ID that exists in
    # a claim; names are minted deliberately via --from-names (TOOLING §5).
    unresolved: dict[str, str | None] = {}
    sources_root = archive_root / 'sources'
    if sources_root.exists():
        for path in sources_root.rglob('*.md'):
            if is_template_file(path):
                continue   # template claims carry teaching placeholders only
            rec = read_record(path, on_decode_error=on_decode_error)
            if rec['undecodable']:
                continue
            for claim in rec['claims']:
                if not isinstance(claim, dict):
                    continue
                for ref in link_field_refs(claim.get('persons')):
                    if id_type_of(ref) != 'P':
                        continue
                    ppid = normalize_id(ref)
                    if ppid not in known_pids and ppid not in unresolved:
                        unresolved[ppid] = None   # name extracted by TODO above

    if undecodable:
        shown = ', '.join(_rel(p, archive_root) for p in undecodable[:5])
        if len(undecodable) > 5:
            shown += f' and {len(undecodable) - 5} more'
        print(
            f'WARNING: {len(undecodable)} file(s) are not saved as UTF-8 text and '
            f'were skipped rather than crashing this scan: {shown}. A stub might '
            'now be (re-)minted for someone who already has a record, or a real '
            "cross-reference inside that file's claims may be missed, because this "
            "run could not read it. The file itself is fine and nothing was "
            'changed - it is only saved in an older encoding (a Windows editor '
            'defaults to one, commonly cp1252). Open it and save it again '
            'choosing UTF-8 (in Notepad: Save As, then pick UTF-8 from the '
            'Encoding menu), then run `fha stubs` again.',
            file=sys.stderr)

    return unresolved


def create_stubs(
    archive_root: Path,
    persons: dict[str, str | None],
    dry_run: bool = False,
) -> int:
    """Create stub files. Returns count of stubs created."""
    stubs_dir = archive_root / 'people' / 'stubs'
    if not dry_run:
        stubs_dir.mkdir(parents=True, exist_ok=True)

    created = 0
    for pid, name in sorted(persons.items()):
        filename = _stub_filename(pid, name)
        stub_path = stubs_dir / filename

        if stub_path.exists():
            continue   # never overwrite

        content = _stub_content(pid, name)
        if dry_run:
            print(f'[dry-run] Would create: people/stubs/{filename}')
        else:
            # Exact + atomic, like every other record writer. The
            # `stub_path.exists()` guard above makes a torn file lower stakes
            # than a truncated biography - the next run skips it rather than
            # repairing it, so the damage is a permanently half-written record
            # nobody notices, which is its own kind of bad. `write_text` would
            # also newline-translate, and a stub is a person record that later
            # grows into a full one.
            write_text_exact_atomic(stub_path, content)
            print(f'Created: people/stubs/{filename}')
        created += 1

    return created


def mint_named_stubs(
    archive_root: Path,
    names: list[str],
    dry_run: bool = False,
) -> None:
    """Mint new P-ids and create stubs for named people."""
    clean_names = [n.strip() for n in names if n.strip()]
    if not clean_names:
        return

    stubs_dir = archive_root / 'people' / 'stubs'
    if not dry_run:
        stubs_dir.mkdir(parents=True, exist_ok=True)

    # Mint all IDs in one call so previews are distinct even in --dry-run: no
    # files are written then, so minting one-per-name would rescan the same tree
    # and could repeat an ID. A single batch dedupes within itself.
    ids = mint_ids('P', len(clean_names), archive_root)

    for name, new_id in zip(clean_names, ids):
        pid = new_id.lower()
        filename = _stub_filename(pid, name)
        stub_path = stubs_dir / filename
        content = _stub_content(pid, name)
        if dry_run:
            print(f'[dry-run] Would create: people/stubs/{filename} ({pid})')
        else:
            # Same reasoning as create_stubs above; this loop has no exists()
            # guard at all, because every P-id in it was just minted fresh.
            write_text_exact_atomic(stub_path, content)
            print(f'Created: people/stubs/{filename} ({pid})')


# ── CLI ───────────────────────────────────────────────────────────────────────

# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Create placeholder records for people who are named but not yet filed.

  fha stubs                          Stub every unresolved person reference
  fha stubs --from-names "A; B; C"   Stub these named people
  fha stubs --dry-run                Preview without writing

Like dropping a blank labeled folder in the cabinet for a name you've heard but
not yet researched. --from-names runs INSTEAD of the reference scan, not with it."""


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'stubs',
        help='Mint person stubs for unresolved P-id references',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.add_argument(
        '--from-names', metavar='NAMES',
        help='Semicolon-separated names to mint IDs and stubs for. Runs INSTEAD '
             'of the unresolved-reference scan, not in addition to it.',
    )
    p.add_argument('--dry-run', action='store_true',
                   help='Preview without writing')
    p.set_defaults(func=_run_stubs)


def _run_stubs(args: argparse.Namespace) -> int:
    root = getattr(args, 'root', None)
    if root:
        archive_root = Path(root).resolve()
    else:
        archive_root = find_archive_root()
        if archive_root is None:
            print(f'ERROR: {archive_root_missing_message()}', file=sys.stderr)
            return EXIT_FAILURE

    dry_run = getattr(args, 'dry_run', False)

    from_names = getattr(args, 'from_names', None)
    if from_names:
        names = [n.strip() for n in from_names.split(';') if n.strip()]
        mint_named_stubs(archive_root, names, dry_run=dry_run)
        return EXIT_CLEAN

    # Default: scan for unresolved P-ids in claims
    unresolved = _collect_unresolved_persons(archive_root)
    if not unresolved:
        print('No unresolved person references found.')
        return EXIT_CLEAN

    count = create_stubs(archive_root, unresolved, dry_run=dry_run)
    if dry_run:
        print(f'[dry-run] Would create {count} stub(s).')
    else:
        print(f'Created {count} stub(s).')
    return EXIT_CLEAN


# ── Standalone ────────────────────────────────────────────────────────────────

def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha stubs',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH')
    parser.add_argument('--from-names', metavar='NAMES')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    return _run_stubs(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
