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
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    archive_relative,
    archive_root_missing_message,
    find_archive_root,
    id_type_of,
    is_template_file,
    link_field_refs,
    load_fha_yaml,
    mint_ids,
    normalize_id,
    parse_filename,
    read_record,
    render_stub_content,
    stub_filename,
    stub_slug_name,
    undecodable_file_recorder,
    name_undecodable_files,
    utf8_resave_remedy,
    write_text_exact_atomic,
)


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
    return render_stub_content(pid, name)


def _collect_unresolved_persons(
    archive_root: Path,
    *,
    unread_people: list | None = None,
    unread_sources: list | None = None,
    unidentified_people: list | None = None,
) -> dict[str, str | None]:
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
    `read_record`'s default `UnicodeDecodeError` take the whole command down.

    The two loops' skips are recorded SEPARATELY, into `unread_people` and
    `unread_sources`, because they do not cost the same thing:

      - a **sources/** file skipped means a P-id referenced only inside it goes
        unseen this run. The scan under-collects, so a stub that was owed is
        not minted. Nothing wrong is written; the run is merely incomplete.
      - a **people/** file skipped would mean an existing person's id is
        missing from `known_pids`, so every claim naming them reads as
        unresolved and `create_stubs` writes a SECOND record for a P-id that
        already has one - the precise corruption `_lib.read_record`'s docstring
        names as worse than the traceback it replaced ("`fha stubs` mints a
        second record for a P-id that already has one").

    So the people/ loop does what `lint._register_unread_record_id` does with
    the same problem: SPEC §13 puts the id in the filename as well as the
    frontmatter, and the filename is bytes this pass can read. A skipped
    person file's id comes off its name and counts as filed, which is the only
    true thing available about it and exactly enough for this scan's one
    question ("who already has a record?"). No content is borrowed - the name
    hint stays absent, as it is for every unresolved id here.

    A skipped person file whose NAME carries no P-id either is the one case
    nothing can answer, and it goes to `unidentified_people`: the caller
    refuses to mint at all rather than minting from a picture of the archive
    it knows is missing someone.

    Reporting belongs to the caller, not here: `_run_stubs` is the layer with
    somewhere to put a report AND the authority to refuse, and a collector that
    printed its own warning while the caller printed the refusal would say the
    same thing twice. Every list is fed through `undecodable_file_recorder`,
    so a file somehow read twice is recorded once.
    """
    unread_people = [] if unread_people is None else unread_people
    unread_sources = [] if unread_sources is None else unread_sources
    unidentified_people = ([] if unidentified_people is None
                           else unidentified_people)
    note_person = undecodable_file_recorder(unread_people)
    note_source = undecodable_file_recorder(unread_sources)
    note_unidentified = undecodable_file_recorder(unidentified_people)

    # Collect all known P-ids from existing person files
    known_pids: set[str] = set()
    people_root = archive_root / 'people'
    if people_root.exists():
        for path in people_root.rglob('*.md'):
            if is_template_file(path):
                continue   # `_TEMPLATE.*` placeholder ids are not real records
            rec = read_record(path, on_decode_error=note_person)
            if rec['undecodable']:
                # Its id off its filename (SPEC §13), the same recovery
                # `lint._register_unread_record_id` makes for the same reason.
                parsed = parse_filename(path)
                named = normalize_id(str(parsed.get('id_str', ''))) if parsed else ''
                if named.startswith('p-'):
                    known_pids.add(named)
                else:
                    note_unidentified(path)
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
            rec = read_record(path, on_decode_error=note_source)
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


def _unread_message(paths: list, archive_root: Path) -> str:
    """The opening sentence both undecodable reports below share.

    Names up to five files the way the human filed them and says what is true
    of every one of them - the file is fine, only its encoding is wrong - so
    each caller adds only the part that differs: what the skip cost, and
    whether the run could go on. Same shape and same remedy wording as
    `fha index`'s undecodable-files warning and `fha lint`'s W128, because it
    is the same condition seen from a third command.
    """
    many = len(paths) != 1
    return (
        name_undecodable_files(paths, archive_root, 'so this run could not read them')
        + (' The files themselves are fine and nothing about them was changed - '
           if many else
           ' The file itself is fine and nothing about it was changed - ')
        + utf8_resave_remedy(plural=many)
    )


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
    unread_people: list[Path] = []
    unread_sources: list[Path] = []
    unidentified_people: list[Path] = []
    unresolved = _collect_unresolved_persons(
        archive_root,
        unread_people=unread_people,
        unread_sources=unread_sources,
        unidentified_people=unidentified_people,
    )

    # A person file that could not be read AND whose filename carries no P-id
    # leaves a hole in the ONE question this command asks - "who already has a
    # record?" - and nothing else in the archive can fill it. Minting anyway
    # writes a second record for a P-id that already has one (#68; see
    # `_collect_unresolved_persons`), so it refuses instead: before any write,
    # and in `--dry-run` too, since a preview drawn from a picture known to be
    # missing someone would promise stubs a real run must not create.
    if unidentified_people:
        print(f'ERROR: {_unread_message(unidentified_people, archive_root)} Their '
              'filenames carry no P-id either, so this run cannot tell whether '
              'the people they hold already have records - and a person whose '
              'record cannot be found looks unfiled, which is how `fha stubs` '
              'would come to write a SECOND record for a P-id that already has '
              'one. Nothing was minted. Re-save the file(s) as UTF-8, then run '
              '`fha stubs` again. (`fha lint` reports the same files as W128.)',
              file=sys.stderr)
        return EXIT_ERRORS

    # A person file that could not be read but IS named for its P-id (SPEC §13,
    # every record the tools write) costs nothing this command needs: its id
    # counted as filed, so no duplicate is minted. Still an incomplete read of
    # the archive, and still worth saying - the human wants to know before the
    # next command needs that file's contents.
    if unread_people:
        print(f'WARNING: {_unread_message(unread_people, archive_root)} Their ids '
              'were read from their filenames instead (SPEC §13), so nobody was '
              'stubbed twice on their account - but nothing inside those records '
              'was read this run. Re-save the file(s) as UTF-8. (`fha lint` '
              'reports the same files as W128.)', file=sys.stderr)

    # A source record that could not be read costs the opposite thing: a P-id
    # referenced only in there goes unseen, so a stub that was owed is simply
    # not minted this run. Nothing wrong is written, so the run proceeds - but
    # it did not see the whole archive, and exit 1 (warnings) is what says so.
    if unread_sources:
        print(f'WARNING: {_unread_message(unread_sources, archive_root)} A person '
              'referenced only inside those file(s) was not seen this run, so a '
              'stub they were owed has not been minted. Re-save the file(s) as '
              'UTF-8 and run `fha stubs` again to pick them up. (`fha lint` '
              'reports the same files as W128.)', file=sys.stderr)

    # Exit 1 whenever this run did NOT see the whole archive (TOOLING §1:
    # "warnings only"). The stubs it minted are right; the set is not
    # certified complete, and a harness reading only the exit code has to be
    # able to tell the difference.
    incomplete = bool(unread_people or unread_sources)

    if not unresolved:
        print('No unresolved person references found.')
        return EXIT_WARNINGS if incomplete else EXIT_CLEAN

    count = create_stubs(archive_root, unresolved, dry_run=dry_run)
    if dry_run:
        print(f'[dry-run] Would create {count} stub(s).')
    else:
        print(f'Created {count} stub(s).')
    return EXIT_WARNINGS if incomplete else EXIT_CLEAN


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
