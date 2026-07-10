"""fha working-copy - manage working-copy mode for a synced archive.

A working copy is a git-synced clone of the archive that contains all records
(sources, people, places, notes) but no binary asset files (photos, documents).
When working-copy mode is active the tools treat absent asset files as
assumed-present-elsewhere rather than missing or lost.

Working-copy mode is flagged by a WORKING_COPY marker file at the archive root.
The marker is listed in .gitignore so it is machine-local and never syncs back
to the main archive.

Sub-commands
  on      Write the WORKING_COPY marker (safe - only withholds asset features).
  off     Remove the WORKING_COPY marker (prompts for confirmation by default).
  status  Report whether this archive is in working-copy mode.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FhaConfigError,
    Result,
    configure_utf8_stdout,
    load_fha_yaml,
    resolve_path,
    resolve_root_arg,
)

# ── constants ─────────────────────────────────────────────────────────────────

_MARKER_NAME = 'WORKING_COPY'

_MARKER_CONTENT = """\
This archive is in working-copy mode.

The photo and document asset files live on the main machine; they are not
present here.  The genealogy tools treat absent asset files as
assumed-present-elsewhere (not missing or lost) and skip asset-mutating
operations (scan, process, packet, tag-person) that would make no sense
without the originals.

Read-only operations - lint, index, report - work normally.

To re-enable full asset features on this machine (e.g. because you have copied
the assets here), run:

    fha working-copy off

Delete this file manually or run that command to turn working-copy mode off.
See SPEC.md §12.4 for the design rationale.
"""

_GITIGNORE_ENTRY = '# Machine-local working-copy marker (git-ignored by design - see SPEC.md §12).\nWORKING_COPY\n'

_OFF_CONFIRM_TEXT = """\
You are about to turn OFF working-copy mode.

After this change the tools will treat missing asset files as real errors
(E011/E012) and asset-mutating commands (process, photoindex scan, etc.) will
be re-enabled.

Only proceed if the asset files (photos and documents) are actually present at
the roots declared in fha.yaml on this machine.

Continue? [y/N] """


# ── helpers ───────────────────────────────────────────────────────────────────

def _marker_path(archive_root: Path) -> Path:
    return archive_root / _MARKER_NAME


def _ensure_gitignore_entry(archive_root: Path) -> None:
    """Make sure the archive's .gitignore contains the WORKING_COPY entry."""
    gi = archive_root / '.gitignore'
    if gi.exists():
        text = gi.read_text(encoding='utf-8')
        # Only count a real ignore rule - skip comment and negation lines so
        # "# remember WORKING_COPY" or "!WORKING_COPY" don't suppress the append.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith('#') and not stripped.startswith('!') and stripped == _MARKER_NAME:
                return
        gi.write_text(text.rstrip('\n') + '\n\n' + _GITIGNORE_ENTRY, encoding='utf-8')
    else:
        gi.write_text(_GITIGNORE_ENTRY, encoding='utf-8')


def _asset_root_risk_notes(archive_root: Path) -> list[str]:
    """Return plain warnings for missing or empty asset roots before mode-off.

    Turning working-copy mode off is the risky direction: it re-enables tools
    that treat missing assets as real absence.  The roots in fha.yaml are the
    human-facing source of where those assets should be, so checking them here
    lets the confirmation prompt say exactly what looks unsafe.
    """
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as exc:
        return [f'fha.yaml could not be read: {exc}']

    notes: list[str] = []
    for alias in ('photos', 'documents'):
        root = resolve_path(alias, fha_config, archive_root)
        if not root.is_dir():
            notes.append(f'the {alias} root {root} is not reachable')
            continue
        try:
            has_anything = any(root.iterdir())
        except OSError:
            notes.append(f'the {alias} root {root} is not readable')
        else:
            if not has_anything:
                notes.append(f'the {alias} root {root} looks empty')
    return notes


def _invalidate_index(archive_root: Path) -> str | None:
    """Delete the query index so exists_on_disk is recomputed after a mode toggle.

    Returns None on success, or an error string if the file exists but cannot
    be removed (permissions, read-only FS, etc.).
    """
    index = archive_root / '.cache' / 'index.sqlite'
    try:
        index.unlink(missing_ok=True)
        return None
    except OSError as exc:
        return str(exc)


def _format_message(msg: object) -> str:
    """Render a Result message whether an older caller supplied text or Message."""
    text = getattr(msg, 'text', str(msg))
    next_step = getattr(msg, 'next_step', None)
    return f'{text} Next: {next_step}' if next_step else text


# ── run_* functions (headless core, return Result) ────────────────────────────

def run_working_copy_on(archive_root: Path) -> Result:
    """Activate working-copy mode."""
    marker = _marker_path(archive_root)
    already = marker.exists()
    result = Result(
        ok=True,
        exit_code=EXIT_CLEAN,
        data={'status': 'already-on' if already else 'turned-on', 'marker': str(marker)},
    )

    if not already:
        try:
            marker.write_text(_MARKER_CONTENT, encoding='utf-8')
        except OSError as exc:
            return Result(
                ok=False,
                exit_code=EXIT_FAILURE,
                data={'status': 'write-failed', 'marker': str(marker)},
            ).add(
                'error',
                f'Could not write the WORKING_COPY marker at {marker}: {exc}',
                next_step='Check folder permissions, then run `fha working-copy on` again.',
            )

    # Always ensure .gitignore is updated - even when the marker was hand-created
    # or a previous run wrote the marker but failed to update .gitignore.
    try:
        _ensure_gitignore_entry(archive_root)
    except OSError as exc:
        result.exit_code = EXIT_WARNINGS
        result.add(
            'warning',
            f'Could not update .gitignore: {exc}',
            next_step='Add WORKING_COPY to .gitignore by hand before syncing this archive.',
        )

    # Invalidate the index so exists_on_disk values are recomputed in WC context.
    index_err = _invalidate_index(archive_root)
    if index_err:
        result.exit_code = EXIT_WARNINGS
        result.add(
            'warning',
            f'Could not remove the stale query index: {index_err}',
            next_step='Run `fha index` manually to rebuild the asset-status index.',
        )

    return result


def run_working_copy_off(archive_root: Path) -> Result:
    """Deactivate working-copy mode (marker already confirmed to exist at call time)."""
    marker = _marker_path(archive_root)
    if not marker.exists():
        return Result(
            ok=True,
            exit_code=EXIT_CLEAN,
            data={'status': 'already-off', 'marker': str(marker)},
            messages=[],
        )
    try:
        marker.unlink()
    except OSError as exc:
        return Result(
            ok=False,
            exit_code=EXIT_FAILURE,
            data={'status': 'write-failed', 'marker': str(marker)},
        ).add(
            'error',
            f'Could not remove the WORKING_COPY marker at {marker}: {exc}',
            next_step='Check folder permissions, then run `fha working-copy off` again.',
        )
    result = Result(
        ok=True,
        exit_code=EXIT_CLEAN,
        data={'status': 'turned-off', 'marker': str(marker)},
        messages=[],
    )
    index_err = _invalidate_index(archive_root)
    if index_err:
        result.exit_code = EXIT_WARNINGS
        result.add(
            'warning',
            f'Could not remove the stale query index: {index_err}',
            next_step='Run `fha index` manually to rebuild the asset-status index.',
        )
    return result


def run_working_copy_status(archive_root: Path) -> Result:
    """Return working-copy mode status."""
    marker = _marker_path(archive_root)
    active = marker.exists()
    return Result(
        ok=True,
        exit_code=EXIT_CLEAN,
        data={'active': active, 'marker': str(marker)},
        messages=[],
    )


# ── _cmd_* functions (CLI rendering layer) ────────────────────────────────────

def _cmd_on(args: argparse.Namespace) -> int:
    configure_utf8_stdout()
    # resolve_root_arg carries the archive guard (validates an explicit --root
    # has fha.yaml at its top and prints its own plain ERROR) and returns None
    # on failure; the caller returns EXIT_FAILURE, matching every other tool.
    archive_root = resolve_root_arg(args, command='fha working-copy')
    if archive_root is None:
        return EXIT_FAILURE

    result = run_working_copy_on(archive_root)

    if not result.ok:
        for msg in result.messages:
            print(f'error: {_format_message(msg)}', file=sys.stderr)
        return result.exit_code

    status = result.data['status']
    if status == 'already-on':
        print('Working-copy mode is already active.')
        print(f'Marker: {result.data["marker"]}')
    else:
        print('Working-copy mode ON.')
        print(f'Marker written: {result.data["marker"]}')
        print()
        print('Asset files (photos, documents) are now treated as assumed-present on the')
        print('main machine. Lint will not report them as missing. Asset-mutating commands')
        print('(process, photoindex scan, packet) are paused on this machine.')
        print()
        print('The query index has been cleared; run `fha index` to rebuild it before')
        print('using index-backed commands (fha find --related, fha views, etc.).')

    for msg in result.messages:
        print(f'note: {_format_message(msg)}')

    return result.exit_code


def _cmd_off(args: argparse.Namespace) -> int:
    configure_utf8_stdout()
    archive_root = resolve_root_arg(args, command='fha working-copy')
    if archive_root is None:
        return EXIT_FAILURE

    from _lib import is_working_copy
    if not is_working_copy(archive_root):
        print('Working-copy mode is already off - nothing to do.')
        return EXIT_CLEAN

    if not args.yes:
        risk_notes = _asset_root_risk_notes(archive_root)
        prompt = _OFF_CONFIRM_TEXT
        if risk_notes:
            prompt = (
                'Warning: the asset roots do not look ready on this machine:\n'
                + ''.join(f'  - {note}\n' for note in risk_notes)
                + '\n'
                + _OFF_CONFIRM_TEXT
            )
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print('\nAborted.')
            return EXIT_CLEAN
        if answer != 'y':
            print('Aborted - working-copy mode remains active.')
            return EXIT_CLEAN

    result = run_working_copy_off(archive_root)

    if not result.ok:
        for msg in result.messages:
            print(f'error: {_format_message(msg)}', file=sys.stderr)
        return result.exit_code

    print('Working-copy mode OFF.')
    print(f'Marker removed: {result.data["marker"]}')
    print()
    print('Asset features are re-enabled. Lint will now report any missing asset files')
    print('as errors. The query index has been cleared; run `fha index` to rebuild it,')
    print('then `fha doctor` to check overall archive health.')
    return result.exit_code


def _cmd_status(args: argparse.Namespace) -> int:
    configure_utf8_stdout()
    archive_root = resolve_root_arg(args, command='fha working-copy')
    if archive_root is None:
        return EXIT_FAILURE

    result = run_working_copy_status(archive_root)
    if result.data['active']:
        print(f'Working-copy mode: ON  (marker: {result.data["marker"]})')
        print()
        print('Asset files (photos, documents) are assumed to live on the main machine.')
        print('Asset-mutating commands are paused here. Read-only commands work normally.')
    else:
        print('Working-copy mode: OFF')
        print(f'No WORKING_COPY marker found at {archive_root}')
        print('This is a full archive - all features are active.')
    return result.exit_code


# ── CLI registration ──────────────────────────────────────────────────────────

def register(subparsers: argparse._SubParsersAction) -> None:
    """Register 'working-copy' onto the main fha parser."""
    p = subparsers.add_parser(
        'working-copy',
        help='Manage working-copy mode (synced archive without asset files).',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root.')
    sub = p.add_subparsers(dest='wc_command', metavar='COMMAND')

    # on
    on_p = sub.add_parser('on', help='Activate working-copy mode.')
    on_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS, help='Archive root.')
    on_p.set_defaults(func=_cmd_on)

    # off
    off_p = sub.add_parser('off', help='Deactivate working-copy mode (prompts for confirmation).')
    off_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS, help='Archive root.')
    off_p.add_argument('--yes', action='store_true',
                       help='Skip the confirmation prompt.')
    off_p.set_defaults(func=_cmd_off)

    # status
    status_p = sub.add_parser('status', help='Report whether working-copy mode is active.')
    status_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS, help='Archive root.')
    status_p.set_defaults(func=_cmd_status)

    p.set_defaults(func=_cmd_status)


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha working-copy',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH')
    sub = parser.add_subparsers(dest='wc_command', metavar='COMMAND')

    on_p = sub.add_parser('on', help='Activate working-copy mode.')
    on_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS)
    on_p.set_defaults(func=_cmd_on)

    off_p = sub.add_parser('off', help='Deactivate working-copy mode.')
    off_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS)
    off_p.add_argument('--yes', action='store_true')
    off_p.set_defaults(func=_cmd_off)

    status_p = sub.add_parser('status', help='Report working-copy mode status.')
    status_p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS)
    status_p.set_defaults(func=_cmd_status)

    parser.set_defaults(func=_cmd_status)
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == '__main__':
    sys.exit(_standalone_main())
