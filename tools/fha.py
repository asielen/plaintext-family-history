#!/usr/bin/env python3
"""
fha — family history archive CLI.

Subcommands live in individual tool files under tools/; each is also
runnable standalone (python tools/lint.py --root ...).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make sure sibling tool modules are importable when this file is run directly
sys.path.insert(0, str(Path(__file__).parent))

import argparse
from _lib import find_archive_root, EXIT_FAILURE


def _require_root(args: argparse.Namespace) -> Path:
    """Resolve the archive root from --root flag or auto-detection."""
    if getattr(args, 'root', None):
        return Path(args.root).resolve()
    detected = find_archive_root()
    if detected is None:
        print('ERROR: cannot find archive root (no fha.yaml found). '
              'Use --root to specify.', file=sys.stderr)
        sys.exit(EXIT_FAILURE)
    return detected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='fha',
        description='Family history archive (fha) tool suite.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Run any subcommand with -h/--help for full options.\n'
            'Documentation: TOOLING.md in the archive root.'
        ),
    )
    parser.add_argument(
        '--root', metavar='PATH',
        help='Archive root (default: auto-detect by walking up from CWD)',
    )
    parser.add_argument(
        '--spec-root', metavar='PATH',
        help='Spec docs root when SPEC.md/TOOLING.md are not in the archive '
             '(e.g. running from the public spec repo)',
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # Import tool modules here (lazy) so that missing deps in one tool
    # don't prevent other tools from loading.
    from id import register as id_register
    from index import register as index_register
    from lint import register as lint_register
    from stubs import register as stubs_register
    from views import register as views_register

    parser = build_parser()
    subs = parser.add_subparsers(dest='command', metavar='COMMAND')

    id_register(subs)
    index_register(subs)
    lint_register(subs)
    stubs_register(subs)
    views_register(subs)

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    return args.func(args) or 0


if __name__ == '__main__':
    sys.exit(main())
