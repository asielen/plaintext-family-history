#!/usr/bin/env python3
"""
fha — family history archive CLI.

Subcommands live in individual tool files under tools/; each is also
runnable standalone (e.g. python tools/lint.py --root …).

This file is intentionally thin — just a dispatcher.  All logic lives in the
individual tool modules.  Adding a new tool: implement it in tools/newtool.py
with a register(subs) function, then add one import + one register() call here.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make sure sibling tool modules are importable when this file is run directly
sys.path.insert(0, str(Path(__file__).parent))

import argparse


def _require_root(args: argparse.Namespace) -> Path:
    """Resolve the archive root from --root flag or auto-detection."""
    from _lib import find_archive_root, EXIT_FAILURE
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
        dest='global_root',
        help='Archive root (default: auto-detect by walking up from CWD)',
    )
    parser.add_argument(
        '--spec-root', metavar='PATH',
        dest='global_spec_root',
        help='Spec docs root when SPEC.md/TOOLING.md are not in the archive '
             '(e.g. running from the public spec repo)',
    )
    return parser


def _intercept_id_check(argv: list[str]) -> int | None:
    """
    Early interception for `fha id check/find <ID> [--root PATH]`.

    The `id check` sub-subparser in id.py does not define --root (id.py stays
    unchanged per TOOLING §4a), so argparse would reject --root when it appears
    after 'check'.  We intercept this specific pattern before argparse sees it
    and dispatch directly to find.find_by_id.

    Returns an exit code when the pattern matches, or None to let normal
    argparse handling proceed.
    """
    # argparse cannot route this alias because id.py intentionally keeps the
    # old implementation unchanged.  Parse just enough of the CLI shape to
    # honor TOOLING §1's dual-position --root convention:
    #   fha --root A id check P-x
    #   fha id --root A check P-x
    #   fha id check P-x --root A
    global_root: str | None = None
    pos = 0
    while pos < len(argv) and argv[pos] in ('--root', '--spec-root'):
        if pos + 1 >= len(argv):
            return None
        if argv[pos] == '--root':
            global_root = argv[pos + 1]
        pos += 2

    if pos >= len(argv) or argv[pos] != 'id':
        return None

    from _lib import FhaConfigError, find_archive_root, load_fha_yaml, normalize_id
    from find import find_by_id as _find_by_id

    rest = argv[pos + 1:]

    # Parse only the arguments we care about for this alias.  The first pass
    # accepts id-level --root before the check/find word; the second accepts
    # root after it.
    id_parser = argparse.ArgumentParser(add_help=False)
    id_parser.add_argument('--root', metavar='PATH')
    id_parser.add_argument('--spec-root', metavar='PATH')
    id_parser.add_argument('id_command', nargs='?')
    id_parsed, tail = id_parser.parse_known_args(rest)
    if id_parsed.id_command not in ('check', 'find'):
        return None

    alias_parser = argparse.ArgumentParser(add_help=False)
    alias_parser.add_argument('id_value', nargs='?', default='')
    alias_parser.add_argument('--root', metavar='PATH')
    alias_parser.add_argument('--spec-root', metavar='PATH')
    parsed, _ = alias_parser.parse_known_args(tail)

    root = parsed.root or id_parsed.root or global_root
    if root:
        archive_root = Path(root).resolve()
    else:
        archive_root = find_archive_root()
        if archive_root is None:
            print('ERROR: cannot find archive root. Use --root.', file=sys.stderr)
            return EXIT_FAILURE

    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE
    return _find_by_id(normalize_id(parsed.id_value), archive_root, fha_config)


def _intercept_doctor(argv: list[str]) -> int | None:
    """
    Early interception for `fha doctor [--root PATH] [--spec-root PATH]`.

    doctor.py guards its own `import yaml` so it can report a missing-PyYAML
    health check cleanly on a fresh machine.  But normal dispatch in main()
    imports every other tool module (id, index, lint, stubs, views, find)
    before doctor.py gets a turn, and those modules import _lib — which
    imports yaml unconditionally — so a missing PyYAML would crash on one of
    those imports before doctor's guard ever runs.  Intercept 'doctor' here,
    before any of those imports happen, and hand off straight to doctor.py's
    own entry point.

    Returns an exit code when the command is doctor, or None to let normal
    argparse handling proceed.
    """
    # Walk argv by hand (rather than stripping every literal 'doctor' token)
    # so a --root value that happens to equal 'doctor' is preserved.
    command_idx: int | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ('--root', '--spec-root'):
            i += 2  # flag + its value
            continue
        if tok.startswith('--root=') or tok.startswith('--spec-root='):
            i += 1
            continue
        command_idx = i
        break

    if command_idx is None or argv[command_idx] != 'doctor':
        return None

    from doctor import _standalone_main as doctor_main
    rest = argv[:command_idx] + argv[command_idx + 1:]
    return doctor_main(rest)


def main(argv: list[str] | None = None) -> int:
    """
    Entry point for `fha` (or `python tools/fha.py`).

    Tool modules are imported inside this function rather than at the top of
    the file so that a syntax error or missing dependency in one tool doesn't
    prevent the other tools from loading.  Each register() call adds that
    tool's subcommand to the shared parser.

    Alias: `fha id check <ID>` is re-routed through find.find_by_id so both
    commands produce the same structured output.  id.py stays unchanged — the
    re-routing is purely in this dispatcher (TOOLING §4a).
    """
    argv_list = list(argv) if argv is not None else sys.argv[1:]

    # Alias interception must happen before argparse because the id check
    # sub-subparser doesn't define --root (id.py is intentionally unchanged).
    result = _intercept_id_check(argv_list)
    if result is not None:
        return result

    # Likewise, intercept 'doctor' before the bulk tool imports below so its
    # guarded yaml check (see _intercept_doctor docstring) gets first crack.
    result = _intercept_doctor(argv_list)
    if result is not None:
        return result

    # Lazy imports: keep them inside main() for the reason above.
    from id import register as id_register
    from index import register as index_register
    from lint import register as lint_register
    from stubs import register as stubs_register
    from views import register as views_register
    from doctor import register as doctor_register
    from find import register as find_register
    from photoindex import register as photoindex_register

    parser = build_parser()
    subs = parser.add_subparsers(dest='command', metavar='COMMAND')

    id_register(subs)
    index_register(subs)
    lint_register(subs)
    stubs_register(subs)
    views_register(subs)
    doctor_register(subs)
    find_register(subs)
    photoindex_register(subs)

    args = parser.parse_args(argv_list)

    if getattr(args, 'root', None) is None:
        args.root = (
            getattr(args, 'views_root', None)
            or getattr(args, 'global_root', None)
        )
    if getattr(args, 'spec_root', None) is None:
        args.spec_root = (
            getattr(args, 'views_spec_root', None)
            or getattr(args, 'global_spec_root', None)
        )

    if not args.command:
        parser.print_help()
        return 0

    return args.func(args) or 0


if __name__ == '__main__':
    sys.exit(main())
