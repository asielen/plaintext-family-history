#!/usr/bin/env python3
"""
id.py — fha id: mint and check archive IDs.

  fha id mint P|S|C|L|H [-n N]      Print fresh IDs (checked for non-existence)
  fha id check <ID>                  Show where an ID appears in the tree

Crockford Base32 alphabet: 0123456789abcdefghjkmnpqrstvwxyz (lowercase;
i l o u omitted to avoid confusion with 1 0 and accidental words).
IDs are immutable, never reused. SPEC §10, TOOLING §4.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    ID_RE,
    EXIT_CLEAN,
    EXIT_FAILURE,
    mint_ids as _shared_mint_ids,
    resolve_root_arg,
    normalize_id,
)



# ── Minting ───────────────────────────────────────────────────────────────────

def mint_ids(
    prefix: str,
    count: int,
    archive_root: Path,
) -> list[str]:
    """Compatibility wrapper around the shared `_lib.mint_ids` implementation."""
    return _shared_mint_ids(prefix, count, archive_root)


# ── Check / locate ────────────────────────────────────────────────────────────

def check_id(id_str: str, archive_root: Path) -> list[tuple[Path, int]]:
    """
    Scan the archive tree for occurrences of `id_str`.
    Returns list of (file_path, line_number) tuples.
    """
    id_norm = normalize_id(id_str)
    hits: list[tuple[Path, int]] = []

    for path in archive_root.rglob('*'):
        if not path.is_file():
            continue
        if path.suffix.lower() not in ('.md', '.yaml', '.yml', '.txt'):
            continue
        try:
            for lineno, line in enumerate(
                path.read_text(encoding='utf-8', errors='ignore').splitlines(),
                start=1,
            ):
                if id_norm in line.lower():
                    hits.append((path, lineno))
        except OSError:
            pass

    return hits


# ── CLI ───────────────────────────────────────────────────────────────────────

def register(subparsers: argparse._SubParsersAction) -> None:
    """Register 'id' subcommands onto the main parser."""
    id_parser = subparsers.add_parser(
        'id',
        help='Mint and check archive IDs',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    id_parser.add_argument('--root', metavar='PATH', help='Archive root')
    id_subs = id_parser.add_subparsers(dest='id_command', metavar='SUBCOMMAND')

    # mint
    mint_p = id_subs.add_parser(
        'mint',
        help='Mint fresh IDs (checked for non-existence)',
        description='Generate one or more fresh IDs of the specified type.',
    )
    mint_p.add_argument(
        'prefix', metavar='TYPE', choices=['P', 'S', 'C', 'L', 'H', 'p', 's', 'c', 'l', 'h'],
        help='ID type: P (person) S (source) C (claim) L (place) H (hypothesis)',
    )
    mint_p.add_argument('-n', type=int, default=1, metavar='N', help='How many IDs to mint (default: 1)')
    # Accept --root after the nested subcommand too (TOOLING §1 dual-position root):
    # fha id mint P --root PATH.  SUPPRESS so an absent flag here doesn't clobber a
    # --root given at the `fha` or `id` level.
    mint_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS, help='Archive root')

    # check (alias: find)
    check_p = id_subs.add_parser(
        'check',
        help='Find where an ID appears in the archive',
        aliases=['find'],
    )
    check_p.add_argument('id_value', metavar='ID', help='ID to locate (e.g. P-de957bcda1)')
    check_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS, help='Archive root')

    id_parser.set_defaults(func=_run_id)


def _run_id(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    sub = getattr(args, 'id_command', None)

    if sub == 'mint':
        if args.n < 1:
            print('ERROR: -n must be at least 1.', file=sys.stderr)
            return EXIT_FAILURE
        try:
            ids = mint_ids(args.prefix, args.n, archive_root)
        except ValueError as e:
            print(f'ERROR: {e}', file=sys.stderr)
            return EXIT_FAILURE
        for i in ids:
            print(i)
        return EXIT_CLEAN

    elif sub in ('check', 'find'):
        id_str = normalize_id(args.id_value)
        if not ID_RE.fullmatch(id_str):
            print(f'ERROR: {args.id_value!r} is not a valid archive ID.', file=sys.stderr)
            return EXIT_FAILURE
        hits = check_id(id_str, archive_root)
        if not hits:
            print(f'{id_str}: not found in archive.')
            return EXIT_CLEAN
        print(f'{id_str}: found in {len(hits)} location(s):')
        for path, lineno in hits:
            rel = path.relative_to(archive_root) if path.is_absolute() else path
            print(f'  {rel}:{lineno}')
        return EXIT_CLEAN

    else:
        # No subcommand — print help
        print('Usage: fha id mint TYPE [-n N] | fha id check ID')
        return EXIT_CLEAN


# ── Standalone entry point ────────────────────────────────────────────────────

def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha id',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH', help='Archive root')
    subs = parser.add_subparsers(dest='id_command', metavar='SUBCOMMAND')

    mint_p = subs.add_parser('mint', help='Mint fresh IDs')
    mint_p.add_argument('prefix', metavar='TYPE', choices=['P', 'S', 'C', 'L', 'H', 'p', 's', 'c', 'l', 'h'])
    mint_p.add_argument('-n', type=int, default=1, metavar='N')
    mint_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS)

    check_p = subs.add_parser('check', help='Find where an ID appears', aliases=['find'])
    check_p.add_argument('id_value', metavar='ID')
    check_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    return _run_id(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
