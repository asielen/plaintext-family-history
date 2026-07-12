#!/usr/bin/env python3
"""
fha - family history archive CLI.

Subcommands live in individual tool files under tools/; each is also
runnable standalone (e.g. python tools/lint.py --root …).

This file is intentionally thin - just a dispatcher.  All logic lives in the
individual tool modules.  Adding a new tool: implement it in tools/newtool.py
with a register(subs) function, then add one import + one register() call here.
"""

from __future__ import annotations

import difflib
import sys
import traceback
from pathlib import Path

# Make sure sibling tool modules are importable when this file is run directly
sys.path.insert(0, str(Path(__file__).parent))

import argparse

COMMANDS = (
    'id', 'index', 'lint', 'check', 'stubs', 'views', 'doctor', 'find', 'search',
    'relate', 'photoindex', 'xref', 'cooccur', 'report', 'packet', 'places',
    'gedcom', 'wikitree', 'process', 'capture', 'convert-mining', 'claim', 'confirm',
    'person', 'source', 'site', 'install', 'update-tools', 'working-copy',
    'normalize-links', 'backup',
)


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
             '(e.g. running from the public spec repo). '
             '(reserved: only `fha lint` reads it yet)',
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Show Python tracebacks for tool-building diagnostics',
    )
    return parser


def _first_command_token(argv: list[str]) -> str | None:
    """Return the first positional token that should be the subcommand.

    The top-level parser has a few global flags that may appear before the
    command. Looking for the command ourselves lets us replace argparse's terse
    "invalid choice" with a "did you mean" hint before argparse exits.
    """
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ('--root', '--spec-root'):
            i += 2
            continue
        if tok in ('--debug', '-h', '--help'):
            i += 1
            continue
        if tok.startswith('--root=') or tok.startswith('--spec-root='):
            i += 1
            continue
        if tok.startswith('-'):
            return None
        return tok
    return None


def _load_site_module():
    """Import tools/site.py under a private module name.

    The tool's command is `fha site`, so its file must be `tools/site.py`
    (BUILD.md M8.1) - but the stem `site` collides with Python's stdlib `site`
    module, which is already in sys.modules from interpreter startup. A plain
    `import site` therefore returns the stdlib module, not ours. Loading the
    file by path under the alias `fha_site` sidesteps the collision without
    disturbing the cached stdlib module the way replacing sys.modules['site']
    would.
    """
    import importlib.util

    mod = sys.modules.get('fha_site')
    if mod is not None:
        return mod
    path = Path(__file__).parent / 'site.py'
    spec = importlib.util.spec_from_file_location('fha_site', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['fha_site'] = mod
    spec.loader.exec_module(mod)
    return mod


def _unknown_command_exit(command: str) -> int:
    """Print a friendly unknown-command message with a close-match suggestion."""
    match = difflib.get_close_matches(command, COMMANDS, n=1)
    if match:
        print(
            f"ERROR: unknown fha command {command!r}. Did you mean `{match[0]}`?\n"
            f"Run `fha {match[0]} --help` to see that command.",
            file=sys.stderr,
        )
    else:
        print(
            f"ERROR: unknown fha command {command!r}. Run `fha --help` to see the command list.",
            file=sys.stderr,
        )
    return 2


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
    while pos < len(argv) and argv[pos] in ('--root', '--spec-root', '--debug'):
        if argv[pos] == '--debug':
            pos += 1
            continue
        if pos + 1 >= len(argv):
            return None
        if argv[pos] == '--root':
            global_root = argv[pos + 1]
        pos += 2

    if pos >= len(argv) or argv[pos] != 'id':
        return None

    from _lib import (
        EXIT_FAILURE,
        FhaConfigError,
        load_fha_yaml,
        normalize_id,
        resolve_root_arg,
    )
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

    # Root resolution and the archive guard (an explicit --root without
    # fha.yaml is almost always a typo'd path, and answering "not found in
    # archive tree" against an empty folder is a false negative the user
    # can't distinguish from a real miss) live in `_lib.resolve_root_arg`,
    # the shared chokepoint. The alias gathers --root from three positions,
    # so hand it over on a minimal namespace.
    root = parsed.root or id_parsed.root or global_root
    archive_root = resolve_root_arg(
        argparse.Namespace(root=root), command='fha id check',
    )
    if archive_root is None:
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
    before doctor.py gets a turn, and those modules import _lib - which
    imports yaml unconditionally - so a missing PyYAML would crash on one of
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
        if tok == '--debug':
            i += 1
            continue
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
    # doctor's own parser only ever accepted `--root`; the global `--spec-root`
    # is reserved for `fha lint` (see the top-level parser's help text) and
    # must not be forwarded here, or doctor_main's argparse rejects it as
    # unrecognized.
    rest = []
    skip_next = False
    for tok in (argv[:command_idx] + argv[command_idx + 1:]):
        if skip_next:
            skip_next = False
            continue
        if tok == '--debug':
            continue
        if tok == '--spec-root':
            skip_next = True
            continue
        if tok.startswith('--spec-root='):
            continue
        rest.append(tok)
    return doctor_main(rest)


def _intercept_scaffold(argv: list[str]) -> int | None:
    """
    Early interception for `fha install …` and `fha update-tools …`.

    scaffold.py only needs stdlib (json, shutil, pathlib) and never imports
    PyYAML.  Without this intercept a user on a fresh machine (PyYAML not yet
    installed) hits the ModuleNotFoundError at the bulk-import block below
    before `fha install` gets a chance to run - which is the very command they
    need to run in order to satisfy that dependency.

    Returns an exit code when the command is install or update-tools, or None
    to let normal argparse handling proceed.
    """
    global_root: str | None = None
    command_idx: int | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == '--debug':
            i += 1
            continue
        if tok in ('--root', '--spec-root'):
            if tok == '--root' and i + 1 < len(argv):
                global_root = argv[i + 1]
            i += 2
            continue
        if tok.startswith('--root='):
            global_root = tok[7:]
            i += 1
            continue
        if tok.startswith('--spec-root='):
            i += 1
            continue
        command_idx = i
        break

    if command_idx is None or argv[command_idx] not in ('install', 'update-tools'):
        return None

    from scaffold import _standalone_main as scaffold_main
    subargv = list(argv[command_idx:])
    # `update-tools` accepts --root as a subcommand flag; inject the global
    # --root (supplied before the command name) when not already present.
    # `install` uses a positional ARCHIVE-PATH, so no injection needed.
    if global_root and subargv[0] == 'update-tools' and '--root' not in subargv:
        subargv = [subargv[0], '--root', global_root] + subargv[1:]
    return scaffold_main(subargv)


def _intercept_gedcom_import(argv: list[str]) -> int | None:
    """
    Early interception for `fha gedcom import <file.ged> …` (TOOLING §13a2).

    The GEDCOM exporter's parser (gedcom.py) takes a positional P-id, so
    argparse would read 'import' as a (bad) P-id. Rather than disturb the
    exporter's surface, the importer is routed here in the dispatcher before
    argparse ever sees it - the same mechanism `fha id check` uses (TOOLING
    §4a). `fha gedcom <P-id> …` continues to behave exactly as before.

    Returns an exit code when the first two command tokens are `gedcom import`,
    or None to let normal argparse handling proceed.
    """
    global_root: str | None = None
    command_idx: int | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == '--debug':
            i += 1
            continue
        if tok in ('--root', '--spec-root'):
            if tok == '--root' and i + 1 < len(argv):
                global_root = argv[i + 1]
            i += 2
            continue
        if tok.startswith('--root='):
            global_root = tok[7:]
            i += 1
            continue
        if tok.startswith('--spec-root='):
            i += 1
            continue
        command_idx = i
        break

    if command_idx is None or argv[command_idx] != 'gedcom':
        return None

    # Skip any flags between 'gedcom' and the next positional (TOOLING §1's
    # dual-position --root convention: `fha gedcom --root A import f.ged`
    # must route here too, not fall through to the exporter's P-id parser).
    j = command_idx + 1
    while j < len(argv):
        tok = argv[j]
        if tok == '--debug':
            j += 1
            continue
        if tok in ('--root', '--spec-root'):
            if tok == '--root' and j + 1 < len(argv):
                global_root = argv[j + 1]
            j += 2
            continue
        if tok.startswith('--root='):
            global_root = tok[7:]
            j += 1
            continue
        if tok.startswith('--spec-root='):
            j += 1
            continue
        break
    if j >= len(argv) or argv[j] != 'import':
        return None

    from gedcom_import import _standalone_main as gedcom_import_main
    subargv = [tok for tok in argv[j + 1:] if tok != '--debug']
    # Honor a --root supplied before the 'import' word when the subcommand
    # didn't set its own.
    if global_root and '--root' not in subargv \
            and not any(t.startswith('--root=') for t in subargv):
        subargv += ['--root', global_root]
    return gedcom_import_main(subargv)


def _intercept_claim_new(argv: list[str]) -> int | None:
    """
    Early interception for `fha claim new …` (SPEC §8.4).

    claim.py's main parser is flat (a positional C-id, plus --status/--value/…
    for the review/field-edit verb). `new` is not a C-id, so letting the flat
    parser see it would misread 'new' as the positional claim_id and fail with
    a confusing "not a valid claim ID" instead of running the mint verb.
    Routed here first instead, the same mechanism `fha gedcom import` and
    `fha id check` use (TOOLING §13a2 / §4a).

    Returns an exit code when the first two command tokens are `claim new`, or
    None to let normal argparse handling proceed - so `fha claim <C-id> …` is
    unaffected and still reaches claim.py's registered subparser below.
    """
    global_root: str | None = None
    command_idx: int | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == '--debug':
            i += 1
            continue
        if tok in ('--root', '--spec-root'):
            if tok == '--root' and i + 1 < len(argv):
                global_root = argv[i + 1]
            i += 2
            continue
        if tok.startswith('--root='):
            global_root = tok[7:]
            i += 1
            continue
        if tok.startswith('--spec-root='):
            i += 1
            continue
        command_idx = i
        break

    if command_idx is None or argv[command_idx] != 'claim':
        return None

    # Skip any flags between 'claim' and the next positional (mirrors
    # _intercept_gedcom_import's dual-position --root convention).
    j = command_idx + 1
    while j < len(argv):
        tok = argv[j]
        if tok == '--debug':
            j += 1
            continue
        if tok in ('--root', '--spec-root'):
            if tok == '--root' and j + 1 < len(argv):
                global_root = argv[j + 1]
            j += 2
            continue
        if tok.startswith('--root='):
            global_root = tok[7:]
            j += 1
            continue
        if tok.startswith('--spec-root='):
            j += 1
            continue
        break
    if j >= len(argv) or argv[j] != 'new':
        return None

    from claim import build_claim_new_parser
    subargv = [tok for tok in argv[j + 1:] if tok != '--debug']
    # Honor a --root supplied before the 'new' word when the subcommand
    # didn't set its own.
    if global_root and '--root' not in subargv \
            and not any(t.startswith('--root=') for t in subargv):
        subargv += ['--root', global_root]
    args = build_claim_new_parser().parse_args(subargv)
    return args.func(args)


def main(argv: list[str] | None = None) -> int:
    """
    Entry point for `fha` (or `python tools/fha.py`).

    Tool modules are imported inside this function rather than at the top of
    the file so that a syntax error or missing dependency in one tool doesn't
    prevent the other tools from loading.  Each register() call adds that
    tool's subcommand to the shared parser.

    Alias: `fha id check <ID>` is re-routed through find.find_by_id so both
    commands produce the same structured output.  id.py stays unchanged - the
    re-routing is purely in this dispatcher (TOOLING §4a).
    """
    argv_list = list(argv) if argv is not None else sys.argv[1:]
    debug = '--debug' in argv_list

    try:
        command = _first_command_token(argv_list)
        if command is not None and command not in COMMANDS:
            return _unknown_command_exit(command)

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

        # Intercept 'install' and 'update-tools' before the bulk imports below.
        # scaffold.py uses only stdlib (json, shutil, pathlib) - it does not need
        # PyYAML.  Without this intercept a user on a fresh machine (PyYAML not yet
        # installed) hits a ModuleNotFoundError before `fha install` can run.
        result = _intercept_scaffold(argv_list)
        if result is not None:
            return result

        # Intercept 'gedcom import' before argparse: the exporter's parser takes
        # a positional P-id and must stay unchanged (TOOLING §13a/§13a2).
        result = _intercept_gedcom_import(argv_list)
        if result is not None:
            return result

        # Intercept 'claim new' before argparse: the review/field-edit parser
        # takes a positional C-id and must stay unchanged (SPEC §8.4).
        result = _intercept_claim_new(argv_list)
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
        from relate import register as relate_register
        from photoindex import register as photoindex_register
        from xref import register as xref_register
        from cooccur import register as cooccur_register
        from report import register as report_register
        from packet import register as packet_register
        from places import register as places_register
        from gedcom import register as gedcom_register
        from wikitree import register as wikitree_register
        from process import register as process_register
        from capture import register as capture_register
        from convert_mining import register as convert_mining_register
        from claim import register as claim_register
        from confirm import register as confirm_register
        from person import register as person_register
        from source import register as source_register
        from scaffold import register as scaffold_register
        from working_copy import register as working_copy_register
        from normalize_links import register as normalize_links_register
        from backup import register as backup_register
        # 'site' shadows Python's stdlib site module (already cached in
        # sys.modules at interpreter startup), so `from site import …` would
        # find the wrong module. Load tools/site.py by path under a private name.
        site_register = _load_site_module().register

        parser = build_parser()
        subs = parser.add_subparsers(dest='command', metavar='COMMAND')

        id_register(subs)
        index_register(subs)
        lint_register(subs)
        stubs_register(subs)
        views_register(subs)
        doctor_register(subs)
        find_register(subs)
        relate_register(subs)
        photoindex_register(subs)
        xref_register(subs)
        cooccur_register(subs)
        report_register(subs)
        packet_register(subs)
        places_register(subs)
        gedcom_register(subs)
        wikitree_register(subs)
        process_register(subs)
        capture_register(subs)
        convert_mining_register(subs)
        claim_register(subs)
        confirm_register(subs)
        person_register(subs)
        source_register(subs)
        site_register(subs)
        scaffold_register(subs)  # adds both 'install' and 'update-tools'
        working_copy_register(subs)
        normalize_links_register(subs)
        backup_register(subs)

        args = parser.parse_args(argv_list)
        debug = bool(getattr(args, 'debug', False))

        if getattr(args, 'root', None) is None:
            args.root = (
                getattr(args, 'views_root', None)
                or getattr(args, 'global_root', None)
            )
        if getattr(args, 'spec_root', None) is None:
            # Only `fha lint` still defines a subcommand --spec-root; every other
            # subcommand's copy was removed (it read nothing). The global
            # `fha --spec-root` position stays, threaded here for lint.
            args.spec_root = getattr(args, 'global_spec_root', None)

        if not args.command:
            parser.print_help()
            return 0

        # Working-copy mode banner: one informational line before any command
        # output so the user knows why asset features are paused.
        # Skip for commands that manage the mode or print their own banner.
        _BANNER_SKIP = {'doctor', 'working-copy', 'install', 'update-tools'}
        if args.command not in _BANNER_SKIP:
            from _lib import find_archive_root, is_working_copy
            _root = getattr(args, 'root', None)
            _ar = Path(_root).resolve() if _root else find_archive_root()
            if _ar and is_working_copy(_ar):
                print(
                    '[working copy] photos and documents live on the main machine'
                    ' - asset features are paused here',
                    file=sys.stderr,
                    flush=True,
                )

        return args.func(args) or 0
    except SystemExit:
        raise
    except ModuleNotFoundError as e:
        if e.name == 'yaml':
            print(
                'ERROR: This tool needs PyYAML to read archive YAML files. '
                'Install it with `python -m pip install pyyaml`, then run `fha doctor`.',
                file=sys.stderr,
            )
            return 3
        if debug:
            traceback.print_exc()
        else:
            print(
                f'ERROR: a required Python module is missing: {e.name}. '
                'Run `fha doctor` to check your archive. Re-run with `--debug` '
                'to show the Python traceback.',
                file=sys.stderr,
            )
        return 3
    except Exception as e:  # noqa: BLE001 - top-level guard for CLI users
        if debug:
            traceback.print_exc()
        else:
            print(
                f'ERROR: something went wrong: {e}\n'
                'Run `fha doctor` to check your archive. Re-run with `--debug` '
                'to show the Python traceback.',
                file=sys.stderr,
            )
        return 3


if __name__ == '__main__':
    sys.exit(main())
