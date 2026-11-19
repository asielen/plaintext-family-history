#!/usr/bin/env python3
"""
build_commands_doc.py - write docs/COMMANDS.md, the one `fha` command reference.

The reference is generated from the argparse definitions the CLI itself
parses with (`tools/fha.py:build_full_parser`, plus the three commands fha.py
routes to their own parsers), so a flag that exists is documented and a flag
that is documented exists. tests/test_commands_doc.py fails when the
committed file differs from what this script writes: after changing any
command's flags, re-run

    py -3.14 build_commands_doc.py

and commit docs/COMMANDS.md with the change.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'docs' / 'COMMANDS.md'

HEADER = """# fha command reference

> **Generated; do not edit.** Written by `build_commands_doc.py` from the
> commands' own definitions, so it always matches the tools. Every command
> also prints this help with `--help` (`fha lint --help`).

Most commands report by default and change nothing until given `--apply`
(or the command's own write flag). Every command accepts `--root PATH` to
name the archive; without it, the archive is found by walking up from the
current folder.
"""


def _extra_parsers() -> list[tuple[str, argparse.ArgumentParser]]:
    """Commands fha.py routes to a parser of their own before argparse runs
    (TOOLING §1): they never appear in the main parser's tree."""
    sys.path.insert(0, str(ROOT / 'tools'))
    from claim import build_claim_new_parser
    from process import build_process_refile_parser
    import gedcom_import

    gi = argparse.ArgumentParser(prog='fha gedcom import',
                                 description=gedcom_import._CLI_DESCRIPTION,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    gedcom_import._add_arguments(gi)
    return [('claim new', build_claim_new_parser()),
            ('process refile', build_process_refile_parser()),
            ('gedcom import', gi)]


def _cell(text: str) -> str:
    return ' '.join(str(text).split()).replace('|', '\\|')


def _options_table(parser: argparse.ArgumentParser) -> list[str]:
    fmt = parser._get_formatter()
    rows = []
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        if action.help == argparse.SUPPRESS:
            continue
        if action.option_strings:
            name = ', '.join(f'`{o}`' for o in action.option_strings)
            if action.nargs != 0:
                name += f' `{fmt._format_args(action, fmt._get_default_metavar_for_optional(action))}`'
        else:
            name = f'`{fmt._format_args(action, fmt._get_default_metavar_for_positional(action))}`'
        help_text = fmt._expand_help(action) if action.help else ''
        rows.append(f'| {name} | {_cell(help_text)} |')
    if not rows:
        return []
    return ['| Argument | What it does |', '|---|---|', *rows, '']


def _subparsers(parser: argparse.ArgumentParser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {a.dest: a.help for a in action._choices_actions}
            seen = set()
            for name, sub in action.choices.items():
                if id(sub) in seen:      # an alias of a command already listed
                    continue
                seen.add(id(sub))
                yield name, sub, helps.get(name)


def _section(path: str, parser: argparse.ArgumentParser, summary: str | None,
             depth: int) -> list[str]:
    out = [f'{"#" * depth} `fha {path}`', '']
    if summary and summary != argparse.SUPPRESS:
        out += [_cell(summary), '']
    if parser.description:
        out += ['```text', parser.description.strip('\n'), '```', '']
    out += _options_table(parser)
    if parser.epilog:
        out += ['```text', parser.epilog.strip('\n'), '```', '']
    for name, sub, sub_help in _subparsers(parser):
        out += _section(f'{path} {name}', sub, sub_help, min(depth + 1, 4))
    return out


def _portable(text: str) -> str:
    """Help texts that name the running interpreter (an install hint built
    from sys.executable, as every dependency message is) would make the file
    differ per machine; the reference says plain `python` instead."""
    for path in {sys.executable, os.path.realpath(sys.executable)}:
        text = text.replace(f'"{path}"', 'python').replace(path, 'python')
    return text


def render() -> str:
    return _portable(_render())


def _render() -> str:
    os.environ['COLUMNS'] = '100'       # argparse wraps by terminal width
    sys.path.insert(0, str(ROOT / 'tools'))
    import fha

    main = fha.build_full_parser()
    lines = [HEADER, '## Options for every command', '']
    lines += _options_table(main)
    commands = list(_subparsers(main))
    extras = dict(_extra_parsers())
    lines += ['## Commands', '']
    lines += [f'- [`fha {name}`](#fha-{name.replace(" ", "-")})' for name, _, _ in commands]
    lines.append('')
    for name, sub, summary in commands:
        lines += _section(name, sub, summary, 2)
        for extra_name, extra in extras.items():
            if extra_name.split()[0] == name:
                lines += _section(extra_name, extra, None, 3)
    return '\n'.join(lines).rstrip() + '\n'


def main() -> int:
    OUT.write_text(render(), encoding='utf-8', newline='\n')
    print(f'Wrote {OUT.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
