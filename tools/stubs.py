#!/usr/bin/env python3
"""
stubs.py — fha stubs: mint person stubs for unresolved P-id references.

  fha stubs                           Scan claims and create missing stubs
  fha stubs --from-names "A; B; C"    Mint new P-ids + stubs for named people
  fha stubs --dry-run                 Preview without writing

Creates {surname}__{given}_{P-id}.md in people/stubs/.
Never overwrites; never moves a stub out of stubs/ (placement is a human act).
TOOLING §5.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    find_archive_root,
    load_fha_yaml,
    normalize_id,
    read_record,
)

import datetime


def _today() -> str:
    return datetime.date.today().isoformat()


def _slug_name(name: str) -> tuple[str, str]:
    """
    Parse a display name into (surname_slug, given_slug) for the filename.
    Best effort: last word = surname, rest = given.
    """
    parts = name.strip().split()
    if not parts:
        return ('unknown', 'unknown')
    if len(parts) == 1:
        return ('unknown', parts[0].lower())
    surname = parts[-1].lower().replace(' ', '_')
    given = '_'.join(p.lower() for p in parts[:-1])
    # Sanitize: only a-z, digits, underscores
    surname = re.sub(r'[^a-z0-9_]', '', surname)
    given = re.sub(r'[^a-z0-9_]', '', given)
    return (surname or 'unknown', given or 'unknown')


def _stub_filename(pid: str, name: str | None) -> str:
    """Generate a stub filename."""
    if name and name.lower() not in ('unknown', ''):
        surname, given = _slug_name(name)
    else:
        surname, given = 'unknown', 'unknown'
    return f'{surname}__{given}_{pid}.md'


def _stub_content(pid: str, name: str | None) -> str:
    display_name = name if name and name.lower() != 'unknown' else 'unknown'
    return (
        f'---\n'
        f'id: {pid}\n'
        f'name: {display_name}\n'
        f'living: unknown\n'
        f'created: {_today()}\n'
        f'tier: stub\n'
        f'---\n'
    )


def _collect_unresolved_persons(archive_root: Path) -> dict[str, str | None]:
    """
    Scan source claims for P-ids that have no person record.
    Returns {pid: name_guess | None}.

    Name guessing is intentionally minimal here: claim values have varied
    structure and reliable name extraction isn't worth the complexity.
    The biographer gives the stub a real name when they promote it from stubs/.
    # TODO: extract name from claim value when claim type is 'relationship'
    #   and the value follows the "{name} is a child of …" pattern — that
    #   would give us a name hint for most auto-generated relationship claims.
    """
    # Collect all known P-ids from existing person files
    known_pids: set[str] = set()
    people_root = archive_root / 'people'
    if people_root.exists():
        for path in people_root.rglob('*.md'):
            rec = read_record(path)
            pid = normalize_id(str(rec['meta'].get('id', '')))
            if pid and pid.startswith('p-'):
                known_pids.add(pid)

    # Scan source claims for P-ids not in known_pids
    unresolved: dict[str, str | None] = {}
    sources_root = archive_root / 'sources'
    if sources_root.exists():
        for path in sources_root.rglob('*.md'):
            rec = read_record(path)
            for claim in rec['claims']:
                if not isinstance(claim, dict):
                    continue
                persons = claim.get('persons') or []
                if isinstance(persons, str):
                    persons = [persons]
                for p_raw in persons:
                    ppid = normalize_id(str(p_raw))
                    if ppid and ppid.startswith('p-') and ppid not in known_pids:
                        if ppid not in unresolved:
                            unresolved[ppid] = None   # name extracted by TODO above

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
            stub_path.write_text(content, encoding='utf-8')
            print(f'Created: people/stubs/{filename}')
        created += 1

    return created


def mint_named_stubs(
    archive_root: Path,
    names: list[str],
    dry_run: bool = False,
) -> None:
    """Mint new P-ids and create stubs for named people."""
    from id import mint_ids

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
            stub_path.write_text(content, encoding='utf-8')
            print(f'Created: people/stubs/{filename} ({pid})')


# ── CLI ───────────────────────────────────────────────────────────────────────

def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'stubs',
        help='Mint person stubs for unresolved P-id references',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.add_argument(
        '--from-names', metavar='NAMES',
        help='Semicolon-separated list of names to mint IDs and stubs for',
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
            print('ERROR: cannot find archive root. Use --root.', file=sys.stderr)
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
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH')
    parser.add_argument('--from-names', metavar='NAMES')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    return _run_stubs(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
