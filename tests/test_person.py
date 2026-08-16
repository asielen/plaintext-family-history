"""
test_person.py - fha person: set-living, relate, estimate, edit, note.

Covers the run_set_living contract (each flip direction on a stub and a curated
profile with a full-text one-line-diff assertion, trailing-comment survival,
CRLF byte-faithfulness, missing-key insertion in stub field order, the
idempotent `already` no-op) and every refusal arm (invalid id shape, unknown
P-id with the `fha find` next step, merged tombstone naming the survivor,
guard-tripping frontmatter left byte-identical). CLI-level checks ride
fha.main: the argparse choices error (exit 2 with the valid list), bare
`fha person` (help + exit 2), and a write under the WORKING_COPY banner.
The end-to-end consumer check flips a person in a copy of the example archive
and asserts `fha index` reflects the new persons.living value.

Also covers the four verbs plan-17 added below set-living: relate (an
unsourced relationships: belief, its reciprocal mirror, idempotency, and the
unrecognised-subtype warning), estimate (provisional birth:/death: writes,
loose-date normalization, the `-` clear, and the soft accepted-claim
warning), and edit/note (the curated profile's Biography/Stories/Research
Notes sections - bounded replace, append, section creation, and the
<!-- private --> redaction-fence checks each verb makes). Every verb repeats
the same five-way check: happy path, --dry-run writes nothing, a missing
person exits 1 with a `fha find` next step, a merged tombstone refuses, and a
CRLF-authored record round-trips with its line endings intact.

Also covers `new` (plan 17 BUILD §3.3 option b, the "+ add person" parity
command): the one-command mint of a brand-new stub. Unlike every verb above,
`new` never locates an existing record - it mints a fresh P-id and writes a
stub via the same `_lib.render_stub_content`/`stub_filename` renderers `fha
stubs` uses. Covered: happy path frontmatter (tier: stub, living: unknown),
each of sex/gender/birth/death individually and combined, the m -> M sex
case fold, the plain refusal for an unrecognised sex (naming the valid
values, no traceback), loose birth wording normalized with a plain gloss,
a nonsense date refused with nothing written or minted, --dry-run writing
nothing (but still drawing a real, unwritten id - matching `fha stubs
--from-names --dry-run`), the mononym filename form the shared slug helper
produces, the never-overwrite guard (forced via a monkeypatched `mint_ids`),
and CLI wiring through `fha.main(['person', 'new', ...])`.

Also pins the ORDER each verb advertises its next steps in (PR #42 round 2).
The advice has to work when followed top to bottom: `set-sex` names `fha index`
before `fha views brackets`, because Ahnentafel placement is derived from the
INDEXED `sex:` and the write has just staled that index; `set-living` names the
rebuild as a precondition of the exports rather than a convenience; and
`set-profile-photo` puts the rebuild ahead of `fha site`, which refuses to build
from a stale index (the workbench refreshes it itself). The last class here is
`SkillInventoryDocsTests`, which holds the shipped docs' skill count and skill
list against `.claude/skills/` itself.

Fixtures only (AGENTS_TOOLING §5): everything runs against temp trees or a
copy of example-archive; the real archive is never touched.
"""

import contextlib
import errno
import io
import os
import pathlib
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import person
from _lib import (
    CACHE_SCHEMA_KEY,
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    INDEX_SCHEMA_VERSION,
    PERSON_SEX_VALUES,
    find_person_record_path,
    load_fha_yaml,
    read_record,
    stub_filename,
)

EXAMPLE = ROOT / 'example-archive'

PID = 'P-aaaaaaaaaa'
CURATED_PID = 'P-cccccccccc'

STUB = (
    '---\n'
    f'id: {PID}\n'
    f'aliases: [{PID}]\n'
    'name: Rose Hartley\n'
    'living: unknown  # not sure yet\n'
    'created: 2026-01-01\n'
    'tier: stub\n'
    '---\n'
    '\n'
    '# Rose Hartley\n'
)

CURATED = (
    '---\n'
    f'id: {CURATED_PID}\n'
    'name: Thomas Hartley\n'
    f'aliases: [{CURATED_PID}, Thomas Hartley]\n'
    'sex: M\n'
    'living: true\n'
    'created: 2026-01-01\n'
    'tier: curated\n'
    '---\n'
    '\n'
    '# Thomas Hartley\n'
    '\n'
    '## Biography\n'
    'Uncited context prose.\n'
)


def _mk_archive(tmp: Path) -> Path:
    """A minimal spec-shaped archive: fha.yaml + people/ (stub and curated)."""
    root = tmp / 'arc'
    (root / 'people' / 'stubs').mkdir(parents=True)
    (root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
    (root / 'people' / 'stubs' / f'hartley__rose_{PID}.md').write_text(
        STUB, encoding='utf-8')
    (root / 'people' / f'hartley__thomas_{CURATED_PID}.md').write_text(
        CURATED, encoding='utf-8')
    return root


# A third curated person - relate's target end. Kept separate from PID/
# CURATED_PID (the set-living fixtures) so relate's tests don't reshape
# fixtures the set-living tests above already depend on byte-for-byte.
TARGET_PID = 'P-bbbbbbbbbb'

TARGET_CURATED = (
    '---\n'
    f'id: {TARGET_PID}\n'
    'name: Margaret Cole\n'
    f'aliases: [{TARGET_PID}, Margaret Cole]\n'
    'sex: F\n'
    'living: false\n'
    'created: 2026-01-01\n'
    'tier: curated\n'
    '---\n'
    '\n'
    '# Margaret Cole\n'
)


def _mk_relate_archive(tmp: Path) -> Path:
    """`_mk_archive` plus a third curated person - relate's target end."""
    root = _mk_archive(tmp)
    (root / 'people' / f'cole__margaret_{TARGET_PID}.md').write_text(
        TARGET_CURATED, encoding='utf-8')
    return root


def _mk_merged_tombstone(root: Path, dead_pid: str = 'P-dddddddddd',
                         survivor_pid: str = CURATED_PID) -> Path:
    """A merged tombstone record naming `survivor_pid`, for the merged-
    tombstone refusal tests every verb below set-living repeats."""
    tomb = (
        '---\n'
        f'id: {dead_pid}\n'
        'name: Old Thomas\n'
        'living: false\n'
        'status: merged\n'
        f'merged_into: {survivor_pid}\n'
        '---\n'
    )
    path = root / 'people' / f'MERGED-INTO-{survivor_pid}__old_{dead_pid}.md'
    path.write_text(tomb, encoding='utf-8')
    return path


def _one_line_diff(before: str, after: str) -> list[tuple[str, str]]:
    """The (before_line, after_line) pairs that differ, positionally."""
    b, a = before.split('\n'), after.split('\n')
    assert len(b) == len(a), 'line count changed'
    return [(x, y) for x, y in zip(b, a) if x != y]


@contextlib.contextmanager
def _write_dies_partway(target: Path, keep: int = 12):
    """Make the next record write die after `keep` characters, as a full disk does.

    The failure being reproduced is not "the edit did not happen" - every verb
    already refuses cleanly and says so. It is the disk filling, or the process
    being killed, between the moment the record is opened for writing and the
    last byte: with a truncating writer the record on disk is then a prefix of
    the replacement, and the verb reports a refusal over a record it has
    already destroyed.

    Both of `_lib`'s record writers end in one `handle.write(text)` on a
    text-mode file object, so intercepting that call reproduces the same
    interruption for either of them. What differs is WHICH file was open at the
    time: `write_text_exact` has the record itself open in truncating mode, so
    the wound lands on the record; `write_text_exact_atomic` has a sibling temp
    file open and the record is not touched until `os.replace`. That is the
    whole difference these tests measure, which is why the interception sits
    this low rather than stubbing out the writer function.
    """
    real_path_open = pathlib.Path.open
    real_fdopen = os.fdopen
    target_key = os.path.abspath(str(target))
    # A folder means "whatever record is written directly into it" - the only
    # way to aim at `new`'s output, whose filename carries an id minted inside
    # the call being interrupted.
    by_parent = os.path.isdir(target_key)

    class _TornHandle:
        def __init__(self, fh):
            self._fh = fh

        def write(self, text):
            self._fh.write(text[:keep])
            raise OSError(errno.ENOSPC, 'No space left on device')

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            with contextlib.suppress(OSError):
                self._fh.close()
            return False

    def _aimed_at(path):
        here = os.path.abspath(str(path))
        return os.path.dirname(here) == target_key if by_parent else here == target_key

    def _patched_path_open(self, mode='r', *a, **kw):
        fh = real_path_open(self, mode, *a, **kw)
        if str(mode).startswith('w') and _aimed_at(self):
            return _TornHandle(fh)
        return fh

    def _patched_fdopen(fd, mode='r', *a, **kw):
        # The atomic writer's temp file is the only fd opened for writing
        # inside the calls these tests wrap.
        fh = real_fdopen(fd, mode, *a, **kw)
        if str(mode).startswith('w'):
            return _TornHandle(fh)
        return fh

    with mock.patch.object(pathlib.Path, 'open', _patched_path_open), \
            mock.patch.object(os, 'fdopen', _patched_fdopen):
        yield


class SetLivingEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name))
        self.stub = self.root / 'people' / 'stubs' / f'hartley__rose_{PID}.md'
        self.curated = self.root / 'people' / f'hartley__thomas_{CURATED_PID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_flip_stub_to_false_changes_exactly_one_line(self) -> None:
        result = person.run_set_living(self.root, PID, 'false')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'ok')
        self.assertEqual(result.data['old'], 'unknown')
        self.assertEqual(result.data['new'], 'false')
        self.assertEqual(result.changed, [str(self.stub)])
        after = self.stub.read_text(encoding='utf-8')
        diffs = _one_line_diff(STUB, after)
        self.assertEqual(diffs, [
            ('living: unknown  # not sure yet', 'living: false  # not sure yet'),
        ])

    def test_flip_curated_each_direction(self) -> None:
        # true -> false -> unknown -> true: each write is one line, each old
        # value is reported, and the consequence line matches the direction.
        for target, old in (('false', 'true'), ('unknown', 'false'), ('true', 'unknown')):
            before = self.curated.read_text(encoding='utf-8')
            result = person.run_set_living(self.root, CURATED_PID, target)
            self.assertEqual(result.exit_code, EXIT_CLEAN, target)
            self.assertEqual(result.data['old'], old)
            after = self.curated.read_text(encoding='utf-8')
            self.assertEqual(_one_line_diff(before, after),
                             [(f'living: {old}', f'living: {target}')])
        # Direction-specific privacy consequence in the output text.
        result = person.run_set_living(self.root, CURATED_PID, 'false')
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('Exports may now include', text)
        result = person.run_set_living(self.root, CURATED_PID, 'true')
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('redacted from every export', text)
        result = person.run_set_living(self.root, CURATED_PID, 'unknown')
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('unknown is treated as living', text)

    def test_success_is_exit_zero_with_index_nudge_as_advice(self) -> None:
        result = person.run_set_living(self.root, PID, 'false')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertTrue(result.ok)
        nudges = [m for m in result.messages if m.next_step == 'fha index']
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0].level, 'info')

    def test_index_nudge_is_not_optional_convenience(self) -> None:
        # Every export reads living: out of the index and refuses on a stale
        # one, so "when convenient" understated it: the rebuild has to happen
        # before the next export, not whenever.
        result = person.run_set_living(self.root, PID, 'false')
        text = ' '.join(m.text for m in result.messages)
        self.assertNotIn('when convenient', text)
        for command in ('fha site', 'fha packet', 'fha gedcom'):
            self.assertIn(command, text)

    def test_value_case_is_normalized(self) -> None:
        result = person.run_set_living(self.root, PID, 'FALSE')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertIn('living: false', self.stub.read_text(encoding='utf-8'))

    def test_crlf_record_churns_only_the_edited_line(self) -> None:
        crlf = STUB.replace('\n', '\r\n')
        self.stub.write_bytes(crlf.encode('utf-8'))
        result = person.run_set_living(self.root, PID, 'false')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        after = self.stub.read_bytes().decode('utf-8')
        b_lines, a_lines = crlf.split('\r\n'), after.split('\r\n')
        self.assertEqual(len(b_lines), len(a_lines))
        diffs = [(x, y) for x, y in zip(b_lines, a_lines) if x != y]
        self.assertEqual(diffs, [
            ('living: unknown  # not sure yet', 'living: false  # not sure yet'),
        ])
        self.assertNotIn('\n', after.replace('\r\n', ''))  # no bare-LF lines crept in

    def test_missing_key_inserted_after_name_in_stub_order(self) -> None:
        no_living = STUB.replace('living: unknown  # not sure yet\n', '')
        self.stub.write_text(no_living, encoding='utf-8')
        import lint
        config = load_fha_yaml(self.root)
        before_codes = {(f.code, f.path) for f in lint._run_lint_core(self.root, config)[0]
                        if f.severity == 'E'}
        result = person.run_set_living(self.root, PID, 'false')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertIsNone(result.data['old'])
        lines = self.stub.read_text(encoding='utf-8').split('\n')
        name_idx = lines.index('name: Rose Hartley')
        self.assertEqual(lines[name_idx + 1], 'living: false')
        rec = read_record(self.stub)
        self.assertEqual(rec['parse_errors'], [])
        self.assertEqual(rec['meta']['living'], 'false')
        # Lint gains no new errors (and the missing-required-field one is gone).
        after_codes = {(f.code, f.path) for f in lint._run_lint_core(self.root, config)[0]
                       if f.severity == 'E'}
        self.assertTrue(after_codes <= before_codes)

    def test_missing_key_and_no_name_inserted_before_closing_fence(self) -> None:
        bare = f'---\nid: {PID}\ncreated: 2026-01-01\n---\n\n# Rose\n'
        self.stub.write_text(bare, encoding='utf-8')
        result = person.run_set_living(self.root, PID, 'unknown')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        lines = self.stub.read_text(encoding='utf-8').split('\n')
        self.assertEqual(lines[3], 'living: unknown')
        self.assertEqual(lines[4], '---')

    def test_already_is_clean_noop(self) -> None:
        before = self.stub.read_bytes()
        result = person.run_set_living(self.root, PID, 'unknown')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'already')
        self.assertEqual(result.changed, [])
        self.assertEqual(self.stub.read_bytes(), before)
        self.assertIn('already living: unknown', result.messages[0].text)

    def test_dry_run_prints_diff_and_writes_nothing(self) -> None:
        before = self.stub.read_bytes()
        result = person.run_set_living(self.root, PID, 'false', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'dry-run')
        self.assertEqual(result.changed, [])
        text = '\n'.join(m.text for m in result.messages)
        self.assertIn('-living: unknown  # not sure yet', text)
        self.assertIn('+living: false  # not sure yet', text)
        self.assertEqual(self.stub.read_bytes(), before)


class SetSexTests(unittest.TestCase):
    """`fha person set-sex` - the 2026-07-26 feedback's item 3: a surgical
    single-line edit for the one frontmatter fact the Ahnentafel derivation
    reads, ending with the brackets/realign nudge that a hand edit could never
    print. Same contract as set-living / set-profile-photo."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name))
        self.stub = self.root / 'people' / 'stubs' / f'hartley__rose_{PID}.md'
        self.curated = self.root / 'people' / f'hartley__thomas_{CURATED_PID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_correcting_a_recorded_value_changes_one_line_and_nudges(self) -> None:
        before = self.curated.read_text(encoding='utf-8')
        result = person.run_set_sex(self.root, CURATED_PID, 'F')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'ok')
        self.assertEqual(result.data['old'], 'M')
        self.assertEqual(result.data['new'], 'F')
        self.assertEqual(result.changed, [str(self.curated)])
        after = self.curated.read_text(encoding='utf-8')
        self.assertEqual(_one_line_diff(before, after), [('sex: M', 'sex: F')])
        text = ' '.join(m.text for m in result.messages)
        # The nudge this verb exists for.
        self.assertIn('fha views brackets', text)
        self.assertIn('--realign', text)
        self.assertIn('fha views brackets', [m.next_step for m in result.messages if m.next_step])

    def test_reindex_is_advertised_before_the_bracket_check(self) -> None:
        # `fha views brackets` derives placement from persons.sex in
        # .cache/index.sqlite (_lib.build_ahnentafel_map), and this write has
        # just staled that index - so a human following the advice in the order
        # printed would read the OLD placement, and --realign refuses outright.
        # The reindex is step one, not an afterthought.
        result = person.run_set_sex(self.root, CURATED_PID, 'F')
        steps = [m.next_step for m in result.messages if m.next_step]
        self.assertEqual(steps.index('fha index'), 0)
        self.assertLess(steps.index('fha index'), steps.index('fha views brackets'))
        text = ' '.join(m.text for m in result.messages)
        self.assertNotIn('when convenient', text)

    def test_case_folds_onto_the_vocabulary(self) -> None:
        for typed, canonical in (('f', 'F'), ('m', 'M'), ('Intersex', 'intersex'),
                                 ('UNKNOWN', 'unknown')):
            result = person.run_set_sex(self.root, CURATED_PID, typed)
            self.assertEqual(result.exit_code, EXIT_CLEAN, typed)
            self.assertEqual(result.data['new'], canonical, typed)
        self.assertIn('sex: unknown\n', self.curated.read_text(encoding='utf-8'))

    def test_absent_key_is_inserted_in_template_order(self) -> None:
        # The stub has no sex: line; it lands after name: and before living:,
        # the person template's order, and the trailing comment on living:
        # survives untouched.
        result = person.run_set_sex(self.root, PID, 'F')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['old'], None)
        after = self.stub.read_text(encoding='utf-8')
        self.assertIn('name: Rose Hartley\nsex: F\nliving: unknown  # not sure yet\n', after)

    def test_already_is_a_no_op(self) -> None:
        before = self.curated.read_bytes()
        result = person.run_set_sex(self.root, CURATED_PID, 'm')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'already')
        self.assertFalse(result.changed)
        self.assertEqual(self.curated.read_bytes(), before)

    def test_dry_run_previews_and_writes_nothing(self) -> None:
        before = self.curated.read_bytes()
        result = person.run_set_sex(self.root, CURATED_PID, 'F', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'dry-run')
        self.assertFalse(result.changed)
        self.assertEqual(self.curated.read_bytes(), before)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('-sex: M', text)
        self.assertIn('+sex: F', text)

    def test_invalid_value_refused_naming_the_vocabulary(self) -> None:
        before = self.curated.read_bytes()
        result = person.run_set_sex(self.root, CURATED_PID, 'male')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')
        self.assertIn('intersex', result.messages[0].text)
        self.assertEqual(self.curated.read_bytes(), before)

    def test_unknown_pid_warns_with_next_step(self) -> None:
        result = person.run_set_sex(self.root, 'P-9zzzzzzzzz', 'F')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.data['status'], 'not-found')
        self.assertIn('fha find', result.messages[0].text)

    def test_merged_tombstone_names_survivor(self) -> None:
        tomb = (
            '---\n'
            'id: P-dddddddddd\n'
            'name: Thomas Hartley\n'
            'sex: M\n'
            'living: false\n'
            'status: merged\n'
            'merged_into: P-cccccccccc\n'
            '---\n'
        )
        path = self.root / 'people' / 'MERGED-INTO-P-cccccccccc__hartley__thomas_P-dddddddddd.md'
        path.write_text(tomb, encoding='utf-8')
        before = path.read_bytes()
        result = person.run_set_sex(self.root, 'P-dddddddddd', 'F')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'merged')
        self.assertIn('fha person set-sex P-cccccccccc F', result.messages[0].text)
        self.assertEqual(path.read_bytes(), before)

    def test_cli_round_trip_via_fha(self) -> None:
        import fha
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = fha.main(['person', 'set-sex', CURATED_PID, 'F',
                           '--root', str(self.root)])
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('sex: F', self.curated.read_text(encoding='utf-8'))
        self.assertIn('fha views brackets', out.getvalue())


class TornWriteTests(unittest.TestCase):
    """A person record is often the archive's only copy of that ancestor, so a
    write that dies partway must leave the OLD bytes, not half a file.

    PR #42 round 5. Every verb here opened the record with a truncating write
    and then wrote into it: a disk that filled, or a process killed, between
    the truncate and the last byte left a prefix of the replacement on disk
    while the verb returned a plain refusal - a message that is a lie about
    the one file it exists to protect. Nothing is recoverable from that; the
    fix is `_lib.write_text_exact_atomic`, whose temp-file-then-`os.replace`
    means a raise leaves the record untouched. These tests measure exactly
    that: interrupt the write, then read the record back.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name))
        self.stub = self.root / 'people' / 'stubs' / f'hartley__rose_{PID}.md'
        self.curated = self.root / 'people' / f'hartley__thomas_{CURATED_PID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_torn_set_sex_write_leaves_the_record_byte_identical(self) -> None:
        before = self.curated.read_bytes()
        with _write_dies_partway(self.curated):
            result = person.run_set_sex(self.root, CURATED_PID, 'F')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')
        self.assertFalse(result.changed)
        # The refusal says nothing was kept, so nothing may have been kept.
        self.assertEqual(self.curated.read_bytes(), before)

    def test_a_torn_write_names_the_file_and_leaves_no_debris(self) -> None:
        before_listing = sorted(p.name for p in self.curated.parent.iterdir())
        with _write_dies_partway(self.curated):
            result = person.run_set_sex(self.root, CURATED_PID, 'F')
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('cannot write', text)
        self.assertIn('retry', text)
        # The atomic writer's temp file is removed on the way out - a stray
        # `.hartley__thomas_….md.xxxx.tmp` in people/ is debris the human
        # would have to recognise and delete himself.
        self.assertEqual(sorted(p.name for p in self.curated.parent.iterdir()),
                         before_listing)

    def _reset_curated(self) -> None:
        """Put the curated record back, with the entry `edit-note` rewrites.

        Each verb in the sweep below needs a whole record to start from. Left
        shared, the first verb's torn write would corrupt the file and every
        later verb would then refuse for the wrong reason - on a record that
        was already ruined - and the sweep would pass while proving nothing.
        """
        self.curated.write_text(CURATED, encoding='utf-8')
        person.run_note(self.root, CURATED_PID, 'stories', 'An older note.')

    def test_every_in_place_verb_survives_a_torn_write(self) -> None:
        # The sweep the finding asked for: one truncating writer left in any
        # of these verbs is the same defect wearing a different verb's name.
        cases = {
            'set-living': lambda: person.run_set_living(
                self.root, CURATED_PID, 'false'),
            'set-profile-photo': lambda: person.run_set_profile_photo(
                self.root, CURATED_PID, 'thomas-portrait.jpg'),
            'set-sex': lambda: person.run_set_sex(self.root, CURATED_PID, 'F'),
            'estimate': lambda: person.run_estimate(
                self.root, CURATED_PID, birth='1870'),
            'edit': lambda: person.run_edit(
                self.root, CURATED_PID, 'biography', text='Replacement prose.'),
            'note': lambda: person.run_note(
                self.root, CURATED_PID, 'research', 'A fresh note.'),
            'edit-note': lambda: person.run_edit_note(
                self.root, CURATED_PID, 'stories',
                old_text='An older note.', text='A corrected note.'),
            'relate': lambda: person.run_relate(
                self.root, CURATED_PID, 'parent', PID),
        }
        for verb, call in cases.items():
            with self.subTest(verb=verb):
                self._reset_curated()
                before = self.curated.read_bytes()
                with _write_dies_partway(self.curated):
                    result = call()
                self.assertEqual(result.data['status'], 'refused', verb)
                self.assertFalse(result.changed, verb)
                self.assertEqual(self.curated.read_bytes(), before, verb)

    def test_new_leaves_no_half_written_stub_behind(self) -> None:
        # `new` cannot truncate an older record - it refuses a collision
        # first - but a torn write there leaves a half-written stub carrying a
        # freshly minted P-id behind a message saying nothing was created, and
        # `fha lint` finds a malformed record nobody knows they made.
        stubs = self.root / 'people' / 'stubs'
        before_listing = sorted(p.name for p in stubs.iterdir())
        # Aimed at the folder, not a filename: `new` mints its P-id inside the
        # call, so the stub's name is not knowable before it is interrupted.
        with _write_dies_partway(stubs):
            result = person.run_new(self.root, 'Alice Hartley')
        self.assertEqual(result.data['status'], 'refused')
        self.assertFalse(result.changed)
        self.assertEqual(sorted(p.name for p in stubs.iterdir()), before_listing)


class SetLivingRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name))
        self.stub = self.root / 'people' / 'stubs' / f'hartley__rose_{PID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_invalid_id_shape_refused(self) -> None:
        result = person.run_set_living(self.root, 'grandma', 'false')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')
        self.assertIn('P-2b3c4d5e6f', result.messages[0].text)  # the example id

    def test_invalid_value_refused_headless(self) -> None:
        # The CLI stops a bad literal at argparse (exit 2); a headless caller
        # gets the same closed set as a plain refusal.
        result = person.run_set_living(self.root, PID, 'deceased')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('true, false, unknown', result.messages[0].text)

    def test_unknown_pid_warns_with_next_step(self) -> None:
        result = person.run_set_living(self.root, 'P-zzzzzzzzzz', 'false')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.data['status'], 'not-found')
        self.assertEqual(result.messages[0].next_step, 'fha find P-zzzzzzzzzz')
        self.assertIn('fha find P-zzzzzzzzzz', result.messages[0].text)

    def test_merged_tombstone_names_survivor(self) -> None:
        tomb = (
            '---\n'
            'id: P-dddddddddd\n'
            'name: Thomas Hartley\n'
            'living: false\n'
            'status: merged\n'
            'merged_into: P-cccccccccc\n'
            '---\n'
        )
        path = self.root / 'people' / 'MERGED-INTO-P-cccccccccc__hartley__thomas_P-dddddddddd.md'
        path.write_text(tomb, encoding='utf-8')
        before = path.read_bytes()
        result = person.run_set_living(self.root, 'P-dddddddddd', 'true')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'merged')
        self.assertIn('fha person set-living P-cccccccccc true', result.messages[0].text)
        self.assertEqual(path.read_bytes(), before)

    def test_hand_edited_merged_status_casing_still_refused(self) -> None:
        # A hand-edited tombstone can carry `status: Merged` (case) or a quoted
        # value with stray whitespace. The guard compares the NORMALIZED status
        # (_lib.is_merged_meta): a casing bypass would write the flag on the
        # tombstone and fork the truth from the surviving record.
        for status_line in ('status: Merged', 'status: " merged "'):
            tomb = (
                '---\n'
                'id: P-dddddddddd\n'
                'name: Thomas Hartley\n'
                'living: false\n'
                f'{status_line}\n'
                'merged_into: P-cccccccccc\n'
                '---\n'
            )
            path = self.root / 'people' / \
                'MERGED-INTO-P-cccccccccc__hartley__thomas_P-dddddddddd.md'
            path.write_text(tomb, encoding='utf-8')
            before = path.read_bytes()
            result = person.run_set_living(self.root, 'P-dddddddddd', 'true')
            self.assertEqual(result.exit_code, EXIT_FAILURE, status_line)
            self.assertEqual(result.data['status'], 'merged', status_line)
            self.assertIn('P-cccccccccc', result.messages[0].text)
            self.assertEqual(path.read_bytes(), before, status_line)
            path.unlink()

    def test_broken_frontmatter_refused_untouched(self) -> None:
        broken = f'---\nid: {PID}\nname: [unterminated\nliving: true\n---\n'
        self.stub.write_text(broken, encoding='utf-8')
        before = self.stub.read_bytes()
        result = person.run_set_living(self.root, PID, 'false')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')
        self.assertEqual(self.stub.read_bytes(), before)

    def test_living_lookalike_in_quoted_scalar_refused_untouched(self) -> None:
        # A multi-line double-quoted scalar can put a column-0 `living:` line
        # inside ANOTHER field's value. Two column-0 candidates = no safe
        # ownership call, so the edit refuses with the file untouched.
        tricky = (
            '---\n'
            f'id: {PID}\n'
            'name: "Rose\n'
            'living: maybe"\n'
            'living: true\n'
            '---\n'
        )
        self.stub.write_text(tricky, encoding='utf-8')
        before = self.stub.read_bytes()
        result = person.run_set_living(self.root, PID, 'false')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')
        self.assertEqual(self.stub.read_bytes(), before)

    def test_single_lookalike_without_real_field_refused_untouched(self) -> None:
        # Only ONE column-0 `living:` line exists, but it sits inside a
        # multi-line quoted scalar - the parsed header has no top-level living
        # field, so editing that line would rewrite the name's value.
        lookalike = (
            '---\n'
            f'id: {PID}\n'
            'name: "Rose\n'
            'living: maybe"\n'
            '---\n'
        )
        self.stub.write_text(lookalike, encoding='utf-8')
        before = self.stub.read_bytes()
        result = person.run_set_living(self.root, PID, 'false')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')
        self.assertEqual(self.stub.read_bytes(), before)

    def test_guard_refuses_corrupting_rewrite(self) -> None:
        # The pre-write guard itself (frontmatter twin of claims_edit_problem):
        # feed it a rewrite whose living value did not land.
        problem = person._frontmatter_edit_problem(
            f'---\nid: {PID}\nliving: true\n---\n',
            expect_living='false', before_meta={'id': PID, 'living': True})
        self.assertIsNotNone(problem)
        self.assertIn('living', problem)
        # ...and a rewrite that silently changed another field's value.
        problem = person._frontmatter_edit_problem(
            f'---\nid: {PID}\nname: Wrong Name\nliving: false\n---\n',
            expect_living='false',
            before_meta={'id': PID, 'name': 'Rose Hartley', 'living': True})
        self.assertIn("'name'", problem)

    def test_flow_style_living_refused_untouched(self) -> None:
        # `living` parses but owns no column-0 line (an exotic one-line shape a
        # hand edit could produce via a nested flow mapping) - refuse, don't guess.
        flow = (
            '---\n'
            f'id: {PID}\n'
            'name: Rose Hartley\n'
            'flags: {living: true}\n'
            '---\n'
        )
        # Here `living` is nested (not top-level), so the key is ABSENT at the
        # top level: the tool inserts a proper top-level line instead. This
        # asserts nested keys are never mistaken for the real field.
        self.stub.write_text(flow, encoding='utf-8')
        result = person.run_set_living(self.root, PID, 'false')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        lines = self.stub.read_text(encoding='utf-8').split('\n')
        self.assertEqual(lines[3], 'living: false')          # inserted after name:
        self.assertIn('flags: {living: true}', lines)        # nested value untouched


class SetLivingCliTests(unittest.TestCase):
    """The argparse boundary and the working-copy banner ride fha.main."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name))
        self.stub = self.root / 'people' / 'stubs' / f'hartley__rose_{PID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        import fha
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_bad_value_literal_is_argparse_exit_2_with_choices(self) -> None:
        import fha
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                fha.main(['person', 'set-living', PID, 'maybe', '--root', str(self.root)])
        self.assertEqual(cm.exception.code, 2)
        text = err.getvalue()
        # Python 3.14 dropped the quotes around argparse choice values
        # ("choose from true, false, unknown"); accept either rendering.
        self.assertRegex(text, r"choose from '?true'?, '?false'?, '?unknown'?")

    def test_bare_person_prints_help_exit_2(self) -> None:
        rc, out, _ = self._run(['person', '--root', str(self.root)])
        self.assertEqual(rc, 2)
        self.assertIn('set-living', out)

    def test_write_succeeds_under_working_copy_banner(self) -> None:
        (self.root / 'WORKING_COPY').write_text('working copy\n', encoding='utf-8')
        rc, out, err = self._run(
            ['person', 'set-living', PID, 'false', '--root', str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn('[working copy]', err)          # the banner announced the mode
        self.assertIn('is now living: false', out)    # ...and the write still landed
        self.assertIn('living: false', self.stub.read_text(encoding='utf-8'))

    def test_uppercase_value_accepted_at_cli(self) -> None:
        rc, out, _ = self._run(
            ['person', 'set-living', PID, 'FALSE', '--root', str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn('living: false', self.stub.read_text(encoding='utf-8'))


class SetLivingIndexRoundTripTests(unittest.TestCase):
    """The consumer chain works end-to-end: flip, reindex, persons.living."""

    @classmethod
    def setUpClass(cls) -> None:
        if not EXAMPLE.is_dir():
            raise unittest.SkipTest('example-archive not present')

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'arc'
        shutil.copytree(EXAMPLE, self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_flip_then_index_reflects_new_value(self) -> None:
        import index
        pid = 'p-3kq9v8x2m1'   # the Caesar stub, living: false in the fixture
        result = person.run_set_living(self.root, pid, 'unknown')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        index.build_index(self.root, load_fha_yaml(self.root))
        conn = sqlite3.connect(self.root / '.cache' / 'index.sqlite')
        try:
            row = conn.execute(
                'SELECT living FROM persons WHERE id = ?', (pid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], 'unknown')


class FindPersonRecordPathTests(unittest.TestCase):
    """The lifted `_lib.find_person_record_path` (shared with confirm draft)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_finds_stub_and_curated_not_companion(self) -> None:
        companion = self.root / 'people' / f'hartley__thomas_timeline_{CURATED_PID}.md'
        companion.write_text('<!-- GENERATED timeline -->\n', encoding='utf-8')
        self.assertEqual(
            find_person_record_path(self.root, PID).name,
            f'hartley__rose_{PID}.md')
        self.assertEqual(
            find_person_record_path(self.root, CURATED_PID).name,
            f'hartley__thomas_{CURATED_PID}.md')

    def test_uppercase_id_resolves(self) -> None:
        self.assertIsNotNone(find_person_record_path(self.root, PID.upper()))

    def test_missing_returns_none(self) -> None:
        self.assertIsNone(find_person_record_path(self.root, 'P-zzzzzzzzzz'))


class NewTests(unittest.TestCase):
    """fha person new: mint one P-id, write its stub under people/stubs/.

    Unlike every other verb in this module, `new` never locates an existing
    record - there is nothing to find yet - so these tests do not reuse the
    merged-tombstone/missing-person fixtures the other verbs share.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name))
        self.stubs_dir = self.root / 'people' / 'stubs'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _existing_stub_names(self) -> set[str]:
        return {p.name for p in self.stubs_dir.iterdir()}

    def test_happy_path_writes_stub_with_expected_frontmatter(self) -> None:
        before = self._existing_stub_names()
        result = person.run_new(self.root, 'Jane Doe')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'ok')
        self.assertEqual(result.data['name'], 'Jane Doe')
        path = Path(result.data['path'])
        self.assertTrue(path.exists())
        self.assertEqual(self._existing_stub_names() - before, {path.name})
        self.assertEqual(result.changed, [str(path)])
        rec = read_record(path)
        self.assertEqual(rec['parse_errors'], [])
        meta = rec['meta']
        self.assertEqual(meta['name'], 'Jane Doe')
        self.assertEqual(meta['tier'], 'stub')
        self.assertEqual(str(meta['living']), 'unknown')
        self.assertEqual(str(meta['id']).lower(), result.data['person_id'].lower())

    def test_each_option_individually(self) -> None:
        cases = [
            ({'sex': 'F'}, {'sex': 'F'}),
            ({'gender': 'non-binary'}, {'gender': 'non-binary'}),
            ({'birth': '1870'}, {'birth': '1870'}),
            ({'death': '1940'}, {'death': '1940'}),
        ]
        for kwargs, expect in cases:
            with self.subTest(kwargs=kwargs):
                result = person.run_new(self.root, 'Option Test', **kwargs)
                self.assertEqual(result.exit_code, EXIT_CLEAN)
                meta = read_record(Path(result.data['path']))['meta']
                for key, value in expect.items():
                    self.assertEqual(str(meta[key]), value)

    def test_all_options_combined(self) -> None:
        result = person.run_new(
            self.root, 'Jordan Rivers', sex='intersex', gender='non-binary',
            birth='1870', death='1940')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        meta = read_record(Path(result.data['path']))['meta']
        self.assertEqual(meta['sex'], 'intersex')
        self.assertEqual(meta['gender'], 'non-binary')
        self.assertEqual(str(meta['birth']), '1870')
        self.assertEqual(str(meta['death']), '1940')

    def test_sex_case_folded_lowercase_m_to_uppercase(self) -> None:
        result = person.run_new(self.root, 'Alex Smith', sex='m')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        meta = read_record(Path(result.data['path']))['meta']
        self.assertEqual(meta['sex'], 'M')

    def test_invalid_sex_refused_plainly_with_no_write(self) -> None:
        before = self._existing_stub_names()
        result = person.run_new(self.root, 'Pat Doe', sex='female')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')
        text = result.messages[0].text
        self.assertIn('gender', text)   # the sex-vs-gender gloss
        for value in sorted(PERSON_SEX_VALUES):
            self.assertIn(value, text)
        self.assertEqual(self._existing_stub_names(), before)   # nothing minted or written

    def test_loose_birth_normalized_with_message(self) -> None:
        result = person.run_new(self.root, 'Rose Cole', birth='circa 1870')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        meta = read_record(Path(result.data['path']))['meta']
        self.assertEqual(str(meta['birth']), '1870~')
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('recorded birth as 1870~ - about 1870', text)

    def test_nonsense_date_refused_without_write(self) -> None:
        before = self._existing_stub_names()
        result = person.run_new(self.root, 'Nonsense Person', birth='blorptown')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('1880', result.messages[0].text)
        self.assertIn('about 1880', result.messages[0].text)
        self.assertEqual(self._existing_stub_names(), before)

    def test_second_date_invalid_writes_nothing(self) -> None:
        # Mirrors estimate's rule: both dates are validated before anything
        # is minted, so a bad second date never leaves a half-written stub.
        before = self._existing_stub_names()
        result = person.run_new(self.root, 'Two Dates', birth='1870', death='nonsense-date')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(self._existing_stub_names(), before)

    def test_dry_run_writes_nothing_but_previews_content(self) -> None:
        before = self._existing_stub_names()
        result = person.run_new(self.root, 'Preview Person', birth='1870', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'dry-run')
        self.assertEqual(result.changed, [])
        self.assertEqual(self._existing_stub_names(), before)
        text = '\n'.join(m.text for m in result.messages)
        self.assertIn('+id:', text)
        self.assertIn('+name: Preview Person', text)
        self.assertIn('+tier: stub', text)
        self.assertIn('+birth: 1870', text)
        self.assertIn('[dry-run] No file written', text)
        # A real (but unwritten) id is still reported in the preview - the
        # same "minted-but-unwritten id" contract stubs.py's dry-run uses.
        self.assertIsNotNone(result.data['person_id'])

    def test_mononym_filename_form(self) -> None:
        result = person.run_new(self.root, 'Cher')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        path = Path(result.data['path'])
        # `fha person new` passes the name straight through the SHARED
        # `_lib.stub_filename` helper. A single-word name is a surname-less
        # mononym, which SPEC §13 files with an EMPTY sort-name slot - the
        # filename leads with the double underscore (`__cher_P-….md`), the
        # same form a hand-filed mononym (e.g. `__caesar_P-….md`) uses.
        self.assertTrue(path.name.startswith('__cher_'))
        self.assertTrue(path.name.endswith('.md'))

    def test_never_overwrites_an_existing_target(self) -> None:
        # mint_ids' own collision scan makes a REAL collision astronomically
        # unlikely, so the guard is exercised directly: monkeypatch mint_ids
        # to hand back an id whose target stub file already exists.
        pid = 'P-eeeeeeeeee'
        filename = stub_filename('Taken Name', pid.lower())
        target = self.stubs_dir / filename
        target.write_text('pre-existing content\n', encoding='utf-8')
        with mock.patch.object(person, 'mint_ids', return_value=[pid]):
            result = person.run_new(self.root, 'Taken Name')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')
        self.assertEqual(target.read_text(encoding='utf-8'), 'pre-existing content\n')

    def test_person_id_override_reuses_a_previously_minted_id(self) -> None:
        # P2 codex finding (round 5, PR #30): the workbench's dry-run preview
        # mints and shows a real P-id, but Apply used to call run_new AGAIN
        # with no override, drawing a second, DIFFERENT id (mint_ids is
        # random) - so the record actually created never matched the one the
        # human approved. `person_id` lets a caller that already minted one
        # (via an earlier dry run) reuse that exact id on the live write.
        preview = person.run_new(self.root, 'Reused Id', dry_run=True)
        self.assertEqual(preview.data['status'], 'dry-run')
        previewed_id = preview.data['person_id']
        live = person.run_new(self.root, 'Reused Id', person_id=previewed_id)
        self.assertEqual(live.exit_code, EXIT_CLEAN)
        self.assertEqual(live.data['person_id'], previewed_id)
        path = Path(live.data['path'])
        self.assertTrue(path.exists())
        self.assertEqual(read_record(path)['meta']['id'].lower(), previewed_id.lower())

    def test_person_id_override_rejects_a_malformed_id(self) -> None:
        result = person.run_new(self.root, 'Bad Id', person_id='not-an-id')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')

    def test_person_id_override_rejects_the_wrong_id_type(self) -> None:
        result = person.run_new(self.root, 'Wrong Type', person_id='S-fa1234567b')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')

    def test_person_id_override_refuses_a_stale_preview_id_that_now_exists(self) -> None:
        # A colliding override (the archive changed since the preview that
        # minted it) must be refused, not silently reused.
        first = person.run_new(self.root, 'First Person')
        self.assertEqual(first.exit_code, EXIT_CLEAN)
        again = person.run_new(self.root, 'Second Person', person_id=first.data['person_id'])
        self.assertEqual(again.exit_code, EXIT_FAILURE)
        self.assertEqual(again.data['status'], 'refused')

    def test_person_id_override_allows_a_claim_named_id_with_no_record(self) -> None:
        # P2 codex finding (round 1, PR #31): the workbench's mint '+' next to
        # a claim-named person passes a P-id that BY DEFINITION already
        # appears textually in a source record's claims - that mention is the
        # reason the stub is being minted, not a staleness signal. Only an
        # existing person RECORD refuses the reuse.
        pid = 'P-ffffffffff'
        src_dir = self.root / 'sources' / 'census'
        src_dir.mkdir(parents=True)
        (src_dir / 'census_S-aaaaaaaaaa.md').write_text(
            '---\nid: S-aaaaaaaaaa\ntitle: A census\n---\n\n## Claims\n\n'
            f'- id: C-aaaaaaaaaa\n  persons: [[[{pid}]]]\n',
            encoding='utf-8')
        result = person.run_new(self.root, 'Claim Named', person_id=pid)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['person_id'], pid)
        path = Path(result.data['path'])
        self.assertTrue(path.exists())
        self.assertEqual(read_record(path)['meta']['id'].lower(), pid.lower())

    def test_blank_name_refused(self) -> None:
        before = self._existing_stub_names()
        result = person.run_new(self.root, '   ')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')
        self.assertEqual(self._existing_stub_names(), before)

    def test_gender_passed_through_verbatim(self) -> None:
        result = person.run_new(self.root, 'Sam Rivers', gender='two-spirit')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        text = Path(result.data['path']).read_text(encoding='utf-8')
        self.assertIn('gender: two-spirit\n', text)


class SetProfilePhotoQuotingTests(unittest.TestCase):
    """The profile_photo value is free text (a filename), so it takes the
    shared yaml_inline quoting rule: a ` #` or `: ` in a common name like
    `Grandpa #2.jpg` written bare would truncate as a YAML comment or corrupt
    the header while the guard ignores the changed field (P2 codex finding,
    round 1, PR #31)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_yaml_significant_filenames_survive_insert_and_replace(self) -> None:
        # First write inserts the key; the second replaces it - both paths
        # must quote. Each value round-trips through the YAML parser intact.
        for value in ('Grandpa #2.jpg', 'photos: 1900/portrait.jpg'):
            with self.subTest(value=value):
                result = person.run_set_profile_photo(self.root, CURATED_PID, value)
                self.assertEqual(result.exit_code, EXIT_CLEAN)
                rec = read_record(Path(result.data['path']))
                self.assertEqual(rec['parse_errors'], [])
                self.assertEqual(rec['meta']['profile_photo'], value)

    def test_plain_filenames_still_written_bare(self) -> None:
        result = person.run_set_profile_photo(self.root, CURATED_PID, 'portrait.jpg')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        text = Path(result.data['path']).read_text(encoding='utf-8')
        self.assertIn('profile_photo: portrait.jpg\n', text)

    def test_site_advice_puts_the_reindex_first(self) -> None:
        # `fha site` opens the index strictly and stops on "index is stale",
        # which this write has just caused; the workbench rebuilds it itself.
        # So the site half of the advice has to name `fha index` first, and
        # the two must not be presented as interchangeable.
        result = person.run_set_profile_photo(self.root, CURATED_PID, 'portrait.jpg')
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('fha index', text)
        self.assertLess(text.index('fha index'), text.index('fha site'))
        self.assertIn('fha index', [m.next_step for m in result.messages if m.next_step])


class RelateTests(unittest.TestCase):
    """fha person relate: an unsourced relationships: belief, both ends."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_relate_archive(Path(self._tmp.name))
        self.stub = self.root / 'people' / 'stubs' / f'hartley__rose_{PID}.md'
        self.curated = self.root / 'people' / f'hartley__thomas_{CURATED_PID}.md'
        self.target = self.root / 'people' / f'cole__margaret_{TARGET_PID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_key_when_absent(self) -> None:
        result = person.run_relate(self.root, CURATED_PID, 'parent', TARGET_PID)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'ok')
        self.assertEqual(result.changed, [str(self.curated)])
        text = self.curated.read_text(encoding='utf-8')
        self.assertIn('relationships:\n', text)
        self.assertIn('  - to: "[[P-bbbbbbbbbb|Margaret Cole]]"\n', text)
        self.assertIn('    type: parent\n', text)
        self.assertIn('    status: hypothesis\n', text)
        self.assertNotIn('subtype:', text)   # omitted when not given

    def test_appends_when_present(self) -> None:
        person.run_relate(self.root, CURATED_PID, 'spouse', TARGET_PID)
        result = person.run_relate(self.root, CURATED_PID, 'parent', PID)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        text = self.curated.read_text(encoding='utf-8')
        self.assertEqual(text.count('  - to:'), 2)
        self.assertIn('type: spouse', text)
        self.assertIn('type: parent', text)

    def test_new_entry_appends_past_a_blank_line_between_entries(self) -> None:
        # A relationships: list whose two existing entries are separated by a
        # blank line: the new entry must land at the TRUE end of the list
        # (after the second entry), never wedged into the blank gap mid-list.
        # The block-end scan advances past a blank run only when the list
        # continues (a later still-indented entry follows).
        text = (
            '---\n'
            f'id: {CURATED_PID}\n'
            'name: Thomas Hartley\n'
            'living: false\n'
            'relationships:\n'
            '  - to: "[[P-1111111111|A One]]"\n'
            '    type: parent\n'
            '    status: hypothesis\n'
            '\n'
            '  - to: "[[P-2222222222|B Two]]"\n'
            '    type: sibling\n'
            '    status: hypothesis\n'
            'created: 2026-01-01\n'
            '---\n'
        )
        item = person._relationship_item_lines('P-3333333333', 'C Three', 'spouse', None)
        new_text = person._insert_relationship_entry(text, item)
        self.assertIsNotNone(new_text)
        lines = new_text.split('\n')
        idx_first = next(i for i, l in enumerate(lines) if 'P-1111111111' in l)
        idx_second = next(i for i, l in enumerate(lines) if 'P-2222222222' in l)
        idx_new = next(i for i, l in enumerate(lines) if 'P-3333333333' in l)
        idx_created = next(i for i, l in enumerate(lines) if l.startswith('created:'))
        # The new entry lands after BOTH existing entries and before created:.
        self.assertLess(idx_first, idx_second)
        self.assertLess(idx_second, idx_new)
        self.assertLess(idx_new, idx_created)
        # All three entries present; the blank line between the first two is
        # left in place (nothing outside the true append point is disturbed).
        self.assertEqual(new_text.count('  - to:'), 3)
        self.assertIn('    status: hypothesis\n\n  - to: "[[P-2222222222|B Two]]"', new_text)

    def test_target_name_with_embedded_quote_is_escaped_not_corrupting(self) -> None:
        # P2 codex-adjacent finding (sweep of PR #30's _lib.py YAML-quoting
        # fix): `target_name` is an EXISTING person's `name:` field, never
        # validated by `relate` itself - a human may have typed a quote into
        # it long before this ran (a nickname: `Anna "Annie" Smith`). Spliced
        # unescaped into the hand-built double-quoted `- to: "[[...]]"`
        # scalar, an embedded `"` used to end the string early and corrupt
        # the rest of the line.
        item = person._relationship_item_lines(
            'P-4444444444', 'Anna "Annie" Smith', 'sibling', None)
        text = '\n'.join(['---', f'id: {CURATED_PID}', 'name: Thomas Hartley',
                          'living: false', 'created: 2026-01-01', '---', ''])
        new_text = person._insert_relationship_entry(text, item)
        self.assertIsNotNone(new_text)
        self.curated.write_text(new_text, encoding='utf-8')
        meta = read_record(self.curated)['meta']
        rel = meta['relationships'][0]
        self.assertEqual(rel['to'], '[[P-4444444444|Anna "Annie" Smith]]')
        self.assertEqual(rel['type'], 'sibling')

    def test_idempotent_duplicate(self) -> None:
        person.run_relate(self.root, CURATED_PID, 'parent', TARGET_PID)
        before = self.curated.read_bytes()
        result = person.run_relate(self.root, CURATED_PID, 'parent', TARGET_PID)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'already')
        self.assertEqual(result.changed, [])
        self.assertEqual(self.curated.read_bytes(), before)

    def test_reciprocal_writes_both_files_with_flipped_type(self) -> None:
        result = person.run_relate(self.root, CURATED_PID, 'parent', PID, reciprocal=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(sorted(result.changed), sorted([str(self.curated), str(self.stub)]))
        curated_text = self.curated.read_text(encoding='utf-8')
        stub_text = self.stub.read_text(encoding='utf-8')
        self.assertIn('type: parent', curated_text)
        self.assertIn('type: child', stub_text)
        self.assertIn(f'[[{CURATED_PID}|Thomas Hartley]]', stub_text)

    def test_reciprocal_second_write_failure_rolls_back_the_first(self) -> None:
        # P2 codex finding (PR #30): the reciprocal write used to write the
        # owner's file, then attempt the target's mirror - and on an OSError
        # there, return a refusal WITHOUT undoing the owner's already-landed
        # write. A --reciprocal call that reports failure must never
        # silently leave a one-sided relationship on disk.
        owner_before = self.curated.read_bytes()
        target_before = self.target.read_bytes()

        calls = {'n': 0}
        real_write = person.write_text_exact_atomic

        def _flaky_write(path, text):
            calls['n'] += 1
            if calls['n'] == 2:
                raise OSError('simulated lock')
            return real_write(path, text)

        with mock.patch.object(person, 'write_text_exact_atomic', _flaky_write):
            result = person.run_relate(
                self.root, CURATED_PID, 'parent', TARGET_PID, reciprocal=True)

        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')
        self.assertEqual(result.changed, [])
        # Both files are exactly as they were - the first write was rolled back.
        self.assertEqual(self.curated.read_bytes(), owner_before)
        self.assertEqual(self.target.read_bytes(), target_before)
        self.assertIn('rolled back', ' '.join(m.text for m in result.messages))

    def test_reciprocal_rerun_fills_in_only_the_missing_mirror(self) -> None:
        # Forward-only first (no --reciprocal); a later --reciprocal call
        # should add just the mirror, not duplicate the forward entry.
        person.run_relate(self.root, CURATED_PID, 'parent', PID)
        curated_before = self.curated.read_bytes()
        result = person.run_relate(self.root, CURATED_PID, 'parent', PID, reciprocal=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.changed, [str(self.stub)])   # only the mirror was written
        self.assertEqual(self.curated.read_bytes(), curated_before)
        self.assertIn('type: child', self.stub.read_text(encoding='utf-8'))

    def test_subtype_written_and_mirrored(self) -> None:
        result = person.run_relate(self.root, CURATED_PID, 'child', PID,
                                   subtype='adoptive', reciprocal=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        curated_text = self.curated.read_text(encoding='utf-8')
        stub_text = self.stub.read_text(encoding='utf-8')
        self.assertIn('subtype: adoptive', curated_text)
        self.assertIn('subtype: adoptive', stub_text)
        self.assertIn('type: child', curated_text)
        self.assertIn('type: parent', stub_text)

    def test_no_status_flag_status_always_hypothesis(self) -> None:
        # Deliberate deviation from the BUILD sketch's open --status flag (see
        # module docstring): relate always writes status: hypothesis, and no
        # --status option is registered on the parser at all.
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        person._add_relate_arguments(sub)
        relate_parser = sub.choices['relate']
        option_strings = {opt for action in relate_parser._actions
                          for opt in action.option_strings}
        self.assertNotIn('--status', option_strings)

        result = person.run_relate(self.root, CURATED_PID, 'parent', TARGET_PID)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertIn('status: hypothesis', self.curated.read_text(encoding='utf-8'))

    def test_unknown_subtype_word_is_a_warning_not_a_refusal(self) -> None:
        result = person.run_relate(self.root, CURATED_PID, 'parent', PID, subtype='made-up')
        self.assertEqual(result.exit_code, EXIT_CLEAN)   # still writes, no exit bump
        self.assertEqual(result.data['status'], 'ok')
        self.assertIn('made-up', self.curated.read_text(encoding='utf-8'))
        warnings = [m for m in result.messages if m.level == 'warning']
        self.assertEqual(len(warnings), 1)
        self.assertIn('kin', warnings[0].text)

    def test_known_subtype_word_no_warning(self) -> None:
        result = person.run_relate(self.root, CURATED_PID, 'parent', PID, subtype='adoptive')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertFalse([m for m in result.messages if m.level == 'warning'])

    def test_self_relation_refused(self) -> None:
        before = self.curated.read_bytes()
        result = person.run_relate(self.root, CURATED_PID, 'parent', CURATED_PID)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'refused')
        self.assertEqual(self.curated.read_bytes(), before)

    def test_dry_run_writes_nothing(self) -> None:
        before = self.curated.read_bytes()
        result = person.run_relate(self.root, CURATED_PID, 'parent', TARGET_PID, dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'dry-run')
        self.assertEqual(result.changed, [])
        self.assertEqual(self.curated.read_bytes(), before)
        text = '\n'.join(m.text for m in result.messages)
        self.assertIn('+  - to:', text)

    def test_missing_person_exit1_next_step(self) -> None:
        result = person.run_relate(self.root, 'P-zzzzzzzzzz', 'parent', TARGET_PID)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.data['status'], 'not-found')
        self.assertEqual(result.messages[0].next_step, 'fha find P-zzzzzzzzzz')

    def test_missing_target_exit1_next_step(self) -> None:
        result = person.run_relate(self.root, CURATED_PID, 'parent', 'P-zzzzzzzzzz')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.data['status'], 'not-found')
        self.assertEqual(result.messages[0].next_step, 'fha find P-zzzzzzzzzz')

    def test_merged_tombstone_owner_side_refused(self) -> None:
        _mk_merged_tombstone(self.root)
        result = person.run_relate(self.root, 'P-dddddddddd', 'parent', TARGET_PID)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'merged')
        self.assertIn(CURATED_PID, result.messages[0].text)

    def test_merged_tombstone_target_side_refused(self) -> None:
        _mk_merged_tombstone(self.root)
        result = person.run_relate(self.root, CURATED_PID, 'parent', 'P-dddddddddd')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'merged')
        self.assertIn(CURATED_PID, result.messages[0].text)

    def test_crlf_file_round_trips_with_endings_intact(self) -> None:
        crlf = CURATED.replace('\n', '\r\n')
        self.curated.write_bytes(crlf.encode('utf-8'))
        result = person.run_relate(self.root, CURATED_PID, 'parent', TARGET_PID)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        after = self.curated.read_bytes().decode('utf-8')
        self.assertNotIn('\n', after.replace('\r\n', ''))
        self.assertIn('\r\n  - to:', after)


class EstimateTests(unittest.TestCase):
    """fha person estimate: provisional, unsourced birth:/death: writes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name))
        self.stub = self.root / 'people' / 'stubs' / f'hartley__rose_{PID}.md'
        self.curated = self.root / 'people' / f'hartley__thomas_{CURATED_PID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_happy_path_inserts_after_living(self) -> None:
        result = person.run_estimate(self.root, PID, birth='1870')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'ok')
        self.assertEqual(result.changed, [str(self.stub)])
        lines = self.stub.read_text(encoding='utf-8').split('\n')
        living_idx = next(i for i, l in enumerate(lines) if l.startswith('living:'))
        self.assertEqual(lines[living_idx + 1], 'birth: 1870')

    def test_normalizes_loose_dates(self) -> None:
        result = person.run_estimate(self.root, PID, birth='circa 1870')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertIn('birth: 1870~', self.stub.read_text(encoding='utf-8'))
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('recorded as 1870~ - about 1870', text)

    def test_clears_with_dash(self) -> None:
        person.run_estimate(self.root, PID, birth='1870')
        result = person.run_estimate(self.root, PID, birth='-')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'ok')
        self.assertNotIn('birth:', self.stub.read_text(encoding='utf-8'))

    def test_clearing_absent_field_is_a_noop(self) -> None:
        before = self.stub.read_bytes()
        result = person.run_estimate(self.root, PID, birth='-')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'already')
        self.assertEqual(result.changed, [])
        self.assertEqual(self.stub.read_bytes(), before)

    def test_errors_plainly_on_nonsense_and_writes_nothing(self) -> None:
        before = self.stub.read_bytes()
        result = person.run_estimate(self.root, PID, birth='blorptown')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('1880', result.messages[0].text)         # a concrete example
        self.assertIn('about 1880', result.messages[0].text)   # the plain-words example too
        self.assertEqual(self.stub.read_bytes(), before)

    def test_neither_flag_refused(self) -> None:
        result = person.run_estimate(self.root, PID)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('--birth', result.messages[0].text)

    def test_both_fields_together_insert_in_order(self) -> None:
        result = person.run_estimate(self.root, PID, birth='1870', death='1940')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        lines = self.stub.read_text(encoding='utf-8').split('\n')
        living_idx = next(i for i, l in enumerate(lines) if l.startswith('living:'))
        self.assertEqual(lines[living_idx + 1], 'birth: 1870')
        self.assertEqual(lines[living_idx + 2], 'death: 1940')

    def test_second_date_invalid_writes_nothing(self) -> None:
        before = self.stub.read_bytes()
        result = person.run_estimate(self.root, PID, birth='1870', death='nonsense-date')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(self.stub.read_bytes(), before)

    def test_already_recorded_is_noop(self) -> None:
        person.run_estimate(self.root, PID, birth='1870')
        before = self.stub.read_bytes()
        result = person.run_estimate(self.root, PID, birth='1870')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'already')
        self.assertEqual(result.changed, [])
        self.assertEqual(self.stub.read_bytes(), before)

    def test_replaces_commented_template_placeholder_line(self) -> None:
        # archive-template ships a commented `# birth: 1840  ...` starter line;
        # estimate should uncomment/replace it, never insert a duplicate key.
        text = STUB.replace(
            'created: 2026-01-01\n',
            '# birth: 1840              # a year, "about 1840"\ncreated: 2026-01-01\n')
        self.stub.write_text(text, encoding='utf-8')
        result = person.run_estimate(self.root, PID, birth='1850')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        after = self.stub.read_text(encoding='utf-8')
        self.assertIn('birth: 1850', after)
        self.assertNotIn('# birth: 1840', after)
        self.assertEqual(after.count('birth:'), 1)

    def test_accepted_claim_warns_but_still_writes(self) -> None:
        self._build_fresh_index_with_accepted_birth_claim()
        result = person.run_estimate(self.root, PID, birth='1875')
        self.assertEqual(result.exit_code, EXIT_CLEAN)   # still writes; no exit bump
        self.assertEqual(result.data['status'], 'ok')
        self.assertIn('birth: 1875', self.stub.read_text(encoding='utf-8'))
        warnings = [m for m in result.messages if m.level == 'warning']
        self.assertEqual(len(warnings), 1)
        self.assertIn('accepted birth claim', warnings[0].text)

    def test_absent_index_no_warning(self) -> None:
        # No .cache/index.sqlite at all - the soft check must degrade
        # silently, never block or warn.
        result = person.run_estimate(self.root, PID, birth='1875')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertFalse([m for m in result.messages if m.level == 'warning'])

    def test_dry_run_writes_nothing(self) -> None:
        before = self.stub.read_bytes()
        result = person.run_estimate(self.root, PID, birth='1870', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'dry-run')
        self.assertEqual(result.changed, [])
        self.assertEqual(self.stub.read_bytes(), before)

    def test_missing_person_exit1_next_step(self) -> None:
        result = person.run_estimate(self.root, 'P-zzzzzzzzzz', birth='1870')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.data['status'], 'not-found')
        self.assertEqual(result.messages[0].next_step, 'fha find P-zzzzzzzzzz')

    def test_merged_tombstone_refused(self) -> None:
        _mk_merged_tombstone(self.root)
        result = person.run_estimate(self.root, 'P-dddddddddd', birth='1870')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'merged')

    def test_crlf_file_round_trips_with_endings_intact(self) -> None:
        crlf = STUB.replace('\n', '\r\n')
        self.stub.write_bytes(crlf.encode('utf-8'))
        result = person.run_estimate(self.root, PID, birth='1870')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        after = self.stub.read_bytes().decode('utf-8')
        self.assertNotIn('\n', after.replace('\r\n', ''))
        self.assertIn('birth: 1870\r\n', after)

    def _build_fresh_index_with_accepted_birth_claim(self) -> None:
        """A minimal, schema-fresh index.sqlite carrying one accepted birth
        claim for PID - just enough for `_accepted_vital_claim_exists` to
        answer True without needing a full `fha index` build."""
        cache_dir = self.root / '.cache'
        cache_dir.mkdir(exist_ok=True)
        conn = sqlite3.connect(cache_dir / 'index.sqlite')
        try:
            conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)')
            conn.execute('INSERT INTO meta VALUES (?, ?)',
                        (CACHE_SCHEMA_KEY, str(INDEX_SCHEMA_VERSION)))
            conn.execute(f'PRAGMA user_version = {INDEX_SCHEMA_VERSION}')
            conn.execute('CREATE TABLE claims(id TEXT PRIMARY KEY, type TEXT, status TEXT)')
            conn.execute('CREATE TABLE claim_persons(claim_id TEXT, person_id TEXT)')
            conn.execute("INSERT INTO claims VALUES ('c-xxxxxxxxxx', 'birth', 'accepted')")
            conn.execute("INSERT INTO claim_persons VALUES ('c-xxxxxxxxxx', ?)", (PID.lower(),))
            conn.commit()
        finally:
            conn.close()


class EditTests(unittest.TestCase):
    """fha person edit: replace (default) or append to one prose section."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name))
        self.stub = self.root / 'people' / 'stubs' / f'hartley__rose_{PID}.md'
        self.curated = self.root / 'people' / f'hartley__thomas_{CURATED_PID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_replaces_only_the_one_section(self) -> None:
        # CURATED only has ## Biography; add a ## Stories section after it so
        # this test can prove the edit stays BOUNDED - the other section
        # (and the frontmatter) survive byte-for-byte.
        multi = CURATED.replace(
            '## Biography\nUncited context prose.\n',
            '## Biography\nUncited context prose.\n\n## Stories\n*(none yet)*\n')
        self.curated.write_text(multi, encoding='utf-8')
        result = person.run_edit(self.root, CURATED_PID, 'biography',
                                 text='New biography prose.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'ok')
        text = self.curated.read_text(encoding='utf-8')
        self.assertIn('## Biography\nNew biography prose.\n\n## Stories', text)
        self.assertIn('id: P-cccccccccc', text)         # frontmatter untouched
        self.assertNotIn('Uncited context prose.', text)  # old prose is gone
        self.assertTrue(text.endswith('*(none yet)*\n'))  # the OTHER section untouched

    def test_append_mode_appends(self) -> None:
        result = person.run_edit(self.root, CURATED_PID, 'biography',
                                 text='A second paragraph.', append=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        text = self.curated.read_text(encoding='utf-8')
        self.assertIn('Uncited context prose.\n\nA second paragraph.', text)

    def test_missing_section_is_created(self) -> None:
        result = person.run_edit(self.root, CURATED_PID, 'research',
                                 text='Open question here.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        text = self.curated.read_text(encoding='utf-8')
        self.assertTrue(text.endswith('## Research Notes\nOpen question here.\n'))

    def test_private_marker_drop_warns_and_exit_warnings(self) -> None:
        text = CURATED.replace(
            '## Biography\nUncited context prose.\n',
            '## Biography\n<!-- private -->\nsecret\n<!-- /private -->\n')
        self.curated.write_text(text, encoding='utf-8')
        result = person.run_edit(self.root, CURATED_PID, 'biography', text='Public only.')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.data['status'], 'ok')       # still writes
        warnings = [m for m in result.messages if m.level == 'warning']
        self.assertEqual(len(warnings), 1)
        self.assertIn('private', warnings[0].text)
        after = self.curated.read_text(encoding='utf-8')
        self.assertIn('Public only.', after)
        self.assertNotIn('secret', after)

    def test_private_marker_kept_no_warning(self) -> None:
        text = CURATED.replace(
            '## Biography\nUncited context prose.\n',
            '## Biography\n<!-- private -->\nsecret\n<!-- /private -->\n')
        self.curated.write_text(text, encoding='utf-8')
        result = person.run_edit(
            self.root, CURATED_PID, 'biography',
            text='<!-- private -->\nsecret\n<!-- /private -->')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertFalse([m for m in result.messages if m.level == 'warning'])

    def test_append_mode_never_warns_about_private_markers(self) -> None:
        # Appending never DROPS the old text, so the fence-drop warning is
        # replace-only (module docstring / run_edit docstring).
        text = CURATED.replace(
            '## Biography\nUncited context prose.\n',
            '## Biography\n<!-- private -->\nsecret\n<!-- /private -->\n')
        self.curated.write_text(text, encoding='utf-8')
        result = person.run_edit(self.root, CURATED_PID, 'biography',
                                 text='More public text.', append=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertFalse([m for m in result.messages if m.level == 'warning'])
        self.assertIn('secret', self.curated.read_text(encoding='utf-8'))

    def test_stub_gets_gentle_note_not_a_refusal(self) -> None:
        result = person.run_edit(self.root, PID, 'biography', text='First bio text.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'ok')
        infos = [m for m in result.messages if m.level == 'info' and 'stub' in m.text]
        self.assertEqual(len(infos), 1)

    def test_curated_tier_gets_no_stub_note(self) -> None:
        result = person.run_edit(self.root, CURATED_PID, 'biography', text='x')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertFalse([m for m in result.messages if 'stub' in m.text])

    def test_dry_run_writes_nothing(self) -> None:
        before = self.curated.read_bytes()
        result = person.run_edit(self.root, CURATED_PID, 'biography',
                                 text='Would-be text.', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'dry-run')
        self.assertEqual(result.changed, [])
        self.assertEqual(self.curated.read_bytes(), before)

    def test_dry_run_shows_private_warning_and_exit_warnings(self) -> None:
        text = CURATED.replace(
            '## Biography\nUncited context prose.\n',
            '## Biography\n<!-- private -->\nsecret\n<!-- /private -->\n')
        self.curated.write_text(text, encoding='utf-8')
        result = person.run_edit(self.root, CURATED_PID, 'biography',
                                 text='Public only.', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.changed, [])
        warnings = [m for m in result.messages if m.level == 'warning']
        self.assertEqual(len(warnings), 1)

    def test_missing_person_exit1_next_step(self) -> None:
        result = person.run_edit(self.root, 'P-zzzzzzzzzz', 'biography', text='x')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.data['status'], 'not-found')
        self.assertEqual(result.messages[0].next_step, 'fha find P-zzzzzzzzzz')

    def test_merged_tombstone_refused(self) -> None:
        _mk_merged_tombstone(self.root)
        result = person.run_edit(self.root, 'P-dddddddddd', 'biography', text='x')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'merged')

    def test_crlf_file_round_trips_with_endings_intact(self) -> None:
        crlf = CURATED.replace('\n', '\r\n')
        self.curated.write_bytes(crlf.encode('utf-8'))
        result = person.run_edit(self.root, CURATED_PID, 'biography', text='New text.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        after = self.curated.read_bytes().decode('utf-8')
        self.assertNotIn('\n', after.replace('\r\n', ''))

    def test_text_and_file_both_given_refused(self) -> None:
        result = person.run_edit(self.root, CURATED_PID, 'biography',
                                 text='a', file_path='b.txt')
        self.assertEqual(result.exit_code, EXIT_FAILURE)

    def test_neither_text_nor_file_refused(self) -> None:
        result = person.run_edit(self.root, CURATED_PID, 'biography')
        self.assertEqual(result.exit_code, EXIT_FAILURE)

    def test_unknown_section_refused(self) -> None:
        result = person.run_edit(self.root, CURATED_PID, 'friends', text='x')
        self.assertEqual(result.exit_code, EXIT_FAILURE)

    def test_file_path_reads_content(self) -> None:
        f = Path(self._tmp.name) / 'story.txt'
        f.write_text('From a file.', encoding='utf-8')
        result = person.run_edit(self.root, CURATED_PID, 'biography', file_path=str(f))
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertIn('From a file.', self.curated.read_text(encoding='utf-8'))

    def test_missing_file_refused(self) -> None:
        result = person.run_edit(self.root, CURATED_PID, 'biography',
                                 file_path=str(Path(self._tmp.name) / 'nope.txt'))
        self.assertEqual(result.exit_code, EXIT_FAILURE)


class NoteTests(unittest.TestCase):
    """fha person note: append-only, creates the section if missing."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name))
        self.stub = self.root / 'people' / 'stubs' / f'hartley__rose_{PID}.md'
        self.curated = self.root / 'people' / f'hartley__thomas_{CURATED_PID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_missing_section(self) -> None:
        result = person.run_note(self.root, PID, 'research', 'First research note.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        text = self.stub.read_text(encoding='utf-8')
        self.assertTrue(text.endswith('## Research Notes\nFirst research note.\n'))

    def test_appends_after_existing_paragraphs(self) -> None:
        person.run_note(self.root, PID, 'research', 'First note.')
        result = person.run_note(self.root, PID, 'research', 'Second note.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        text = self.stub.read_text(encoding='utf-8')
        self.assertIn('First note.\n\nSecond note.', text)

    def test_replaces_placeholder_not_appends_after_it(self) -> None:
        # Give the curated fixture a Stories section holding the
        # archive-template placeholder ("*(none yet)*") - note should treat
        # it as empty rather than appending after it.
        text = CURATED + '\n## Stories\n*(none yet)*\n'
        self.curated.write_text(text, encoding='utf-8')
        result = person.run_note(self.root, CURATED_PID, 'stories', 'A real story.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        out = self.curated.read_text(encoding='utf-8')
        self.assertIn('## Stories\nA real story.', out)
        self.assertNotIn('none yet', out)

    def test_never_touches_existing_text(self) -> None:
        person.run_note(self.root, PID, 'research', 'Keep me exactly as written.')
        person.run_note(self.root, PID, 'research', 'Add me too.')
        after = self.stub.read_text(encoding='utf-8')
        self.assertIn('Keep me exactly as written.', after)
        self.assertIn('Add me too.', after)

    def test_biography_section_refused(self) -> None:
        # note only ever adds to stories/research (module docstring) -
        # biography is edit's replace-by-default territory.
        result = person.run_note(self.root, CURATED_PID, 'biography', 'x')
        self.assertEqual(result.exit_code, EXIT_FAILURE)

    def test_unclosed_private_fence_refused(self) -> None:
        text = CURATED + '\n## Stories\n<!-- private -->\nunclosed\n'
        self.curated.write_text(text, encoding='utf-8')
        before = self.curated.read_bytes()
        result = person.run_note(self.root, CURATED_PID, 'stories', 'New text.')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('unclosed', result.messages[0].text)
        self.assertEqual(self.curated.read_bytes(), before)

    def test_balanced_private_fence_not_refused(self) -> None:
        text = CURATED + '\n## Stories\n<!-- private -->\nclosed fine\n<!-- /private -->\n'
        self.curated.write_text(text, encoding='utf-8')
        result = person.run_note(self.root, CURATED_PID, 'stories', 'New text.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)

    def test_empty_text_refused(self) -> None:
        result = person.run_note(self.root, PID, 'research', '   ')
        self.assertEqual(result.exit_code, EXIT_FAILURE)

    def test_dry_run_writes_nothing(self) -> None:
        before = self.stub.read_bytes()
        result = person.run_note(self.root, PID, 'research', 'Note text.', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'dry-run')
        self.assertEqual(result.changed, [])
        self.assertEqual(self.stub.read_bytes(), before)

    def test_missing_person_exit1_next_step(self) -> None:
        result = person.run_note(self.root, 'P-zzzzzzzzzz', 'research', 'x')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertEqual(result.data['status'], 'not-found')
        self.assertEqual(result.messages[0].next_step, 'fha find P-zzzzzzzzzz')

    def test_merged_tombstone_refused(self) -> None:
        _mk_merged_tombstone(self.root)
        result = person.run_note(self.root, 'P-dddddddddd', 'research', 'x')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(result.data['status'], 'merged')

    def test_crlf_file_round_trips_with_endings_intact(self) -> None:
        crlf = STUB.replace('\n', '\r\n')
        self.stub.write_bytes(crlf.encode('utf-8'))
        result = person.run_note(self.root, PID, 'research', 'Note text.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        after = self.stub.read_bytes().decode('utf-8')
        self.assertNotIn('\n', after.replace('\r\n', ''))


class EditNoteTests(unittest.TestCase):
    """fha person edit-note: rewrite ONE append-log entry, matched by exact text."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_archive(Path(self._tmp.name))
        self.stub = self.root / 'people' / 'stubs' / f'hartley__rose_{PID}.md'
        person.run_note(self.root, PID, 'research', 'First note.')
        person.run_note(self.root, PID, 'research', 'Second note.')
        person.run_note(self.root, PID, 'research', 'Third note.')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rewrites_only_the_named_entry(self) -> None:
        result = person.run_edit_note(
            self.root, PID, 'research', 'Second note.', 'Second note, corrected.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        text = self.stub.read_text(encoding='utf-8')
        self.assertIn('First note.\n\nSecond note, corrected.\n\nThird note.', text)
        self.assertNotIn('Second note.\n', text.replace('Second note, corrected.', ''))

    def test_entry_not_found_refused_nothing_written(self) -> None:
        before = self.stub.read_bytes()
        result = person.run_edit_note(
            self.root, PID, 'research', 'Never written.', 'x')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('not found', result.messages[0].text)
        self.assertEqual(self.stub.read_bytes(), before)

    def test_duplicate_entry_refused_as_ambiguous(self) -> None:
        person.run_note(self.root, PID, 'research', 'First note.')  # a duplicate
        before = self.stub.read_bytes()
        result = person.run_edit_note(self.root, PID, 'research', 'First note.', 'x')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('2 times', result.messages[0].text)
        self.assertEqual(self.stub.read_bytes(), before)

    def test_empty_replacement_refused(self) -> None:
        result = person.run_edit_note(self.root, PID, 'research', 'First note.', '   ')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('replacement text was empty', result.messages[0].text)

    def test_dry_run_writes_nothing_and_shows_diff(self) -> None:
        before = self.stub.read_bytes()
        result = person.run_edit_note(
            self.root, PID, 'research', 'First note.', 'Changed.', dry_run=True)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'dry-run')
        self.assertEqual(result.changed, [])
        self.assertEqual(self.stub.read_bytes(), before)
        joined = '\n'.join(m.text for m in result.messages)
        self.assertIn('+Changed.', joined)

    def test_biography_section_refused(self) -> None:
        result = person.run_edit_note(self.root, PID, 'biography', 'a', 'b')
        self.assertEqual(result.exit_code, EXIT_FAILURE)

    def test_unbalanced_brackets_warn(self) -> None:
        result = person.run_edit_note(
            self.root, PID, 'research', 'First note.', 'See [[S-2b3c4d5e6f.')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertTrue(any('[[' in m.text for m in result.messages))

    def test_crlf_file_round_trips_with_endings_intact(self) -> None:
        crlf = self.stub.read_text(encoding='utf-8').replace('\n', '\r\n')
        self.stub.write_bytes(crlf.encode('utf-8'))
        result = person.run_edit_note(
            self.root, PID, 'research', 'Second note.', 'Second note, corrected.')
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        after = self.stub.read_bytes().decode('utf-8')
        self.assertNotIn('\n', after.replace('\r\n', ''))
        self.assertIn('Second note, corrected.', after)


class PersonNewVerbsCliTests(unittest.TestCase):
    """CLI wiring smoke tests for new/relate/estimate/edit/note via fha.main."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _mk_relate_archive(Path(self._tmp.name))
        self.stub = self.root / 'people' / 'stubs' / f'hartley__rose_{PID}.md'
        self.curated = self.root / 'people' / f'hartley__thomas_{CURATED_PID}.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        import fha
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_new_cli_writes(self) -> None:
        stubs_dir = self.root / 'people' / 'stubs'
        before = {p.name for p in stubs_dir.iterdir()}
        rc, out, _ = self._run(
            ['person', 'new', 'Jamie Fox', '--sex', 'f', '--birth', '1870',
             '--root', str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn('Created', out)
        new_files = {p.name for p in stubs_dir.iterdir()} - before
        self.assertEqual(len(new_files), 1)
        text = (stubs_dir / new_files.pop()).read_text(encoding='utf-8')
        self.assertIn('name: Jamie Fox', text)
        self.assertIn('sex: F', text)
        self.assertIn('birth: 1870', text)

    def test_new_requires_name_at_cli(self) -> None:
        import fha
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                fha.main(['person', 'new', '--root', str(self.root)])
        self.assertEqual(cm.exception.code, 2)

    def test_new_dry_run_cli_writes_nothing(self) -> None:
        stubs_dir = self.root / 'people' / 'stubs'
        before = {p.name for p in stubs_dir.iterdir()}
        rc, out, _ = self._run(
            ['person', 'new', 'Preview Person', '--dry-run', '--root', str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn('[dry-run]', out)
        self.assertEqual({p.name for p in stubs_dir.iterdir()}, before)

    def test_new_invalid_sex_at_cli_is_a_plain_refusal_not_argparse_error(self) -> None:
        # --sex has no argparse choices= (the plain-language, gender-glossed
        # refusal comes from run_new instead) - this is a normal exit 3, not
        # an argparse exit 2.
        rc, out, err = self._run(
            ['person', 'new', 'Pat Doe', '--sex', 'female', '--root', str(self.root)])
        self.assertEqual(rc, 3)
        self.assertIn('gender', err)

    def test_relate_cli_writes(self) -> None:
        rc, out, _ = self._run(
            ['person', 'relate', CURATED_PID, '--parent', TARGET_PID, '--root', str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn('Recorded', out)
        self.assertIn('type: parent', self.curated.read_text(encoding='utf-8'))

    def test_relate_requires_exactly_one_relation_flag(self) -> None:
        import fha
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                fha.main(['person', 'relate', CURATED_PID, '--root', str(self.root)])
        self.assertEqual(cm.exception.code, 2)

    def test_relate_rejects_two_relation_flags(self) -> None:
        import fha
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                fha.main(['person', 'relate', CURATED_PID, '--parent', TARGET_PID,
                          '--spouse', TARGET_PID, '--root', str(self.root)])
        self.assertEqual(cm.exception.code, 2)

    def test_estimate_cli_writes(self) -> None:
        rc, out, _ = self._run(
            ['person', 'estimate', PID, '--birth', '1870', '--root', str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn('birth: 1870', self.stub.read_text(encoding='utf-8'))

    def test_edit_cli_writes(self) -> None:
        rc, out, _ = self._run(
            ['person', 'edit', CURATED_PID, '--section', 'biography',
             '--text', 'CLI text.', '--root', str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn('CLI text.', self.curated.read_text(encoding='utf-8'))

    def test_edit_text_and_file_mutually_exclusive_at_cli(self) -> None:
        import fha
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                fha.main(['person', 'edit', CURATED_PID, '--section', 'biography',
                          '--text', 'a', '--file', 'b.txt', '--root', str(self.root)])
        self.assertEqual(cm.exception.code, 2)

    def test_edit_requires_text_or_file_at_cli(self) -> None:
        import fha
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                fha.main(['person', 'edit', CURATED_PID, '--section', 'biography',
                          '--root', str(self.root)])
        self.assertEqual(cm.exception.code, 2)

    def test_edit_requires_section_at_cli(self) -> None:
        import fha
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                fha.main(['person', 'edit', CURATED_PID, '--text', 'x', '--root', str(self.root)])
        self.assertEqual(cm.exception.code, 2)

    def test_note_cli_writes(self) -> None:
        rc, out, _ = self._run(
            ['person', 'note', PID, '--section', 'research',
             '--text', 'CLI note.', '--root', str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn('CLI note.', self.stub.read_text(encoding='utf-8'))

    def test_note_rejects_biography_section_at_cli(self) -> None:
        import fha
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                fha.main(['person', 'note', PID, '--section', 'biography',
                          '--text', 'x', '--root', str(self.root)])
        self.assertEqual(cm.exception.code, 2)

    def test_note_requires_text_at_cli(self) -> None:
        import fha
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                fha.main(['person', 'note', PID, '--section', 'research', '--root', str(self.root)])
        self.assertEqual(cm.exception.code, 2)

    def test_group_help_lists_all_six_verbs(self) -> None:
        rc, out, _ = self._run(['person', '--root', str(self.root)])
        self.assertEqual(rc, 2)
        for verb in ('new', 'promote', 'set-living', 'relate', 'estimate', 'edit', 'note'):
            self.assertIn(verb, out)


# ── promote ───────────────────────────────────────────────────────────────────

# The promote fixture family: a small direct line derived from accepted
# relationship claims. KID (curated, position 1) anchors root_person; PA/MA
# are the parents (positions 2/3), GPA the paternal grandfather (position 4),
# FRIEND is off the line, and TOMB is a merged tombstone.
P_KID = 'P-1aaaaaaaaa'
P_PA = 'P-1bbbbbbbbb'
P_MA = 'P-1ccccccccc'
P_GPA = 'P-1ddddddddd'
P_FRIEND = 'P-1eeeeeeeee'
P_TOMB = 'P-1fffffffff'
S_REL = 'S-1aaaaaaaaa'
PROMOTE_FOLDER = '002 Pa Line + Ma Line'


def _promote_person_text(pid: str, name: str, sex: str = 'U', tier: str = 'stub') -> str:
    return (f'---\nid: {pid}\nname: {name}\nsex: {sex}\nliving: false\n'
            f'tier: {tier}\n---\n\n# {name}\n\n## Biography\n\nx\n')


def _promote_rel_claim(cid: str, child: str, parents: list[str]) -> str:
    plist = ', '.join(parents)
    persons = ', '.join([child] + parents)
    return (
        f'- value: "{child} child of {plist}"\n'
        f'  id: {cid}\n  type: relationship\n  subtype: biological\n'
        f'  persons: [{persons}]\n  roles:\n'
        f'    child: {child}\n    parent: [{plist}]\n'
        f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
        f'  information: primary\n  evidence: direct\n  notes: x.\n'
    )


class PromoteTests(unittest.TestCase):
    """`fha person promote` - the stub -> curated graduation verb.

    Happy path (tier flipped surgically, record moved into the derived couple
    folder, research companion scaffolded from the template), the missing-
    folder creation path, --dry-run zero-writes, rollback when a step fails
    partway, and every refusal arm: unknown id (exit 1 + `fha find`), invalid
    id shape, merged tombstone, already-curated no-op (exit 0), the v1
    non-direct refusal (plain words, stub stays a legitimate state), missing
    root_person, missing index, and --into validation. Fixtures only.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'people' / PROMOTE_FOLDER).mkdir(parents=True)
        (self.root / 'sources' / 'notes').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text(
            f'root_person: {P_KID}\nroots:\n  documents: documents\n',
            encoding='utf-8')
        (self.root / 'people' / PROMOTE_FOLDER / f'line__kid_{P_KID}.md').write_text(
            _promote_person_text(P_KID, 'Kid Line', 'F', 'curated'), encoding='utf-8')
        self.pa_stub = self.root / 'people' / 'stubs' / f'line__pa_{P_PA}.md'
        self.pa_stub.write_text(
            _promote_person_text(P_PA, 'Pa Line', 'M'), encoding='utf-8')
        (self.root / 'people' / 'stubs' / f'line__ma_{P_MA}.md').write_text(
            _promote_person_text(P_MA, 'Ma Line', 'F'), encoding='utf-8')
        (self.root / 'people' / 'stubs' / f'line__gpa_{P_GPA}.md').write_text(
            _promote_person_text(P_GPA, 'Gpa Line', 'M'), encoding='utf-8')
        (self.root / 'people' / 'stubs' / f'far__frank_{P_FRIEND}.md').write_text(
            _promote_person_text(P_FRIEND, 'Frank Far', 'M'), encoding='utf-8')
        (self.root / 'people' / 'stubs' / f'gone__tom_{P_TOMB}.md').write_text(
            f'---\nid: {P_TOMB}\nname: Tom Gone\nliving: false\ntier: stub\n'
            f'status: merged\nmerged_into: {P_PA}\n---\n', encoding='utf-8')
        claims = (_promote_rel_claim('C-1aaaaaaaaa', P_KID, [P_PA, P_MA])
                  + _promote_rel_claim('C-1bbbbbbbbb', P_PA, [P_GPA]))
        (self.root / 'sources' / 'notes' / f'rel_{S_REL.lower()}.md').write_text(
            f'---\nid: {S_REL}\ntitle: Rel\nsource_type: other\n---\n\n'
            f'## Claims\n```yaml\n{claims}```\n', encoding='utf-8')
        self._reindex()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _reindex(self) -> None:
        import index as index_mod
        index_mod.build_index(self.root, load_fha_yaml(self.root))

    def _tree_snapshot(self) -> dict[str, bytes]:
        return {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in sorted((self.root / 'people').rglob('*.md'))
        }

    def test_happy_path_flips_moves_and_scaffolds(self) -> None:
        res = person.run_promote(self.root, P_PA)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertEqual(res.data['status'], 'ok')
        self.assertEqual(res.data['position'], 2)
        self.assertEqual(res.data['folder'], PROMOTE_FOLDER)
        # The record left stubs/ and landed in the existing 002 folder.
        self.assertFalse(self.pa_stub.exists())
        new_path = self.root / 'people' / PROMOTE_FOLDER / f'line__pa_{P_PA}.md'
        self.assertTrue(new_path.exists())
        rec = read_record(new_path)
        self.assertEqual(str(rec['meta'].get('tier')), 'curated')
        # Only the tier line changed - name/sex/living survive byte-faithfully.
        self.assertIn('name: Pa Line', new_path.read_text(encoding='utf-8'))
        # The research companion exists, scaffolded from the template grammar.
        research = self.root / 'people' / PROMOTE_FOLDER / f'line__pa_research_{P_PA}.md'
        self.assertTrue(research.exists())
        text = research.read_text(encoding='utf-8')
        self.assertIn(f'id: {P_PA}', text)
        for heading in ('## Research Notes', '## Open Questions',
                        '## Hypotheses', '## Research Log'):
            self.assertIn(heading, text)
        self.assertNotIn('P-__________', text)
        # Follow-ups name the three views; the index was updated IN PLACE
        # (#37) - it still exists, is fresh, and already knows the new path
        # and tier, so the next promote needs no rebuild between them.
        all_text = ' '.join(m.text for m in res.messages)
        for follow in ('fha views timeline', 'fha views sources-index',
                       'fha views draft-queue', 'updated in place'):
            self.assertIn(follow, all_text)
        db = self.root / '.cache' / 'index.sqlite'
        self.assertTrue(db.exists())
        conn = sqlite3.connect(db)
        try:
            tier, ppath = conn.execute(
                'SELECT tier, path FROM persons WHERE id=?', (P_PA.lower(),)).fetchone()
            self.assertEqual(tier, 'curated')
            self.assertEqual(Path(ppath), new_path.relative_to(self.root))
            files = dict(conn.execute(
                'SELECT kind, path FROM person_files WHERE person_id=?', (P_PA.lower(),)).fetchall())
            self.assertEqual(Path(files['profile']), new_path.relative_to(self.root))
            self.assertEqual(Path(files['research']), research.relative_to(self.root))
            self.assertFalse(any('stubs' in Path(v).parts for v in files.values()))
        finally:
            conn.close()
        # And the freshness check agrees: no reader sees it as stale.
        from _lib import newest_record_mtime
        self.assertGreaterEqual(db.stat().st_mtime, newest_record_mtime(self.root))

    def test_two_promotes_back_to_back_need_no_reindex(self) -> None:
        # The #37 batch: promote deleted the cache, so the SECOND promote died
        # on 'index.sqlite is unreadable or has an incompatible schema (schema
        # version is missing)' - reads like corruption - or on a root_person
        # error pointing at fha.yaml. In-place relocation keeps it fresh.
        first = person.run_promote(self.root, P_PA)
        self.assertEqual(first.exit_code, EXIT_CLEAN, first.messages)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            second = person.run_promote(self.root, P_MA)
        self.assertEqual(second.exit_code, EXIT_CLEAN, second.messages)
        self.assertEqual(second.data['status'], 'ok')
        self.assertNotIn('schema version is missing', err.getvalue())
        self.assertTrue(
            (self.root / 'people' / PROMOTE_FOLDER / f'line__ma_{P_MA}.md').exists())

    def test_empty_index_names_fha_index_not_fha_yaml(self) -> None:
        # An index file that exists but holds no persons (created empty, or
        # never fully built) used to produce 'root_person ... has no person
        # record ... fix root_person in fha.yaml or run fha stubs' - a message
        # capable of prompting damage in response to a non-problem (#37).
        db = self.root / '.cache' / 'index.sqlite'
        conn = sqlite3.connect(db)
        conn.execute('DELETE FROM persons')
        conn.commit()
        conn.close()
        # Keep it 'fresh' so the empty-persons branch is what fires.
        os.utime(db, None)
        with contextlib.redirect_stderr(io.StringIO()):
            res = person.run_promote(self.root, P_PA)
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        msg = res.messages[-1].text
        self.assertIn('no person records at all', msg)
        self.assertIn('fha index', msg)
        self.assertNotIn('fha stubs', msg)
        self.assertTrue(self.pa_stub.exists())

    def test_creates_missing_couple_folder(self) -> None:
        res = person.run_promote(self.root, P_GPA)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertEqual(res.data['position'], 4)
        folder = self.root / 'people' / '004 Gpa Line'
        self.assertTrue((folder / f'line__gpa_{P_GPA}.md').exists())
        self.assertTrue((folder / f'line__gpa_research_{P_GPA}.md').exists())

    def test_dry_run_writes_nothing(self) -> None:
        before = self._tree_snapshot()
        res = person.run_promote(self.root, P_PA, dry_run=True)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertEqual(res.data['status'], 'dry-run')
        self.assertEqual(self._tree_snapshot(), before)
        self.assertTrue((self.root / '.cache' / 'index.sqlite').exists())
        self.assertFalse(res.changed)
        preview = ' '.join(m.text for m in res.messages)
        self.assertIn('tier: stub -> curated', preview)
        self.assertIn('research companion', preview)

    def test_rolls_back_when_the_move_fails(self) -> None:
        before = self._tree_snapshot()
        with mock.patch('_lib.shutil.move', side_effect=OSError('disk full')):
            res = person.run_promote(self.root, P_PA)
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertEqual(res.data['status'], 'refused')
        # The tier flip was undone; the tree is byte-identical to before.
        self.assertEqual(self._tree_snapshot(), before)
        self.assertIn('tier: stub', self.pa_stub.read_text(encoding='utf-8'))

    def test_unknown_person_exits_1_with_find(self) -> None:
        res = person.run_promote(self.root, 'P-9zzzzzzzzz')
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertEqual(res.data['status'], 'not-found')
        self.assertIn('fha find', res.messages[0].text)

    def test_invalid_id_refused(self) -> None:
        res = person.run_promote(self.root, 'nonsense')
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertEqual(res.data['status'], 'refused')

    def test_merged_tombstone_refused(self) -> None:
        res = person.run_promote(self.root, P_TOMB)
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertEqual(res.data['status'], 'merged')

    def test_already_curated_is_a_clean_no_op(self) -> None:
        res = person.run_promote(self.root, P_KID)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertEqual(res.data['status'], 'already')
        self.assertIn(PROMOTE_FOLDER, res.messages[0].text)
        self.assertFalse(res.changed)

    def test_non_direct_person_refused_plainly(self) -> None:
        res = person.run_promote(self.root, P_FRIEND)
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertEqual(res.data['status'], 'refused')
        msg = res.messages[0].text
        self.assertIn('direct', msg)
        self.assertIn('legitimate', msg)
        # The stub is untouched.
        self.assertTrue(
            (self.root / 'people' / 'stubs' / f'far__frank_{P_FRIEND}.md').exists())

    def test_missing_root_person_refused(self) -> None:
        (self.root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        res = person.run_promote(self.root, P_PA)
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertIn('root_person', res.messages[0].text)
        self.assertTrue(self.pa_stub.exists())

    def test_missing_index_refused_naming_fha_index(self) -> None:
        (self.root / '.cache' / 'index.sqlite').unlink()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            res = person.run_promote(self.root, P_PA)
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertIn('fha index', res.messages[0].text)
        self.assertTrue(self.pa_stub.exists())

    def test_into_absolute_path_refused(self) -> None:
        res = person.run_promote(self.root, P_PA, into=str(self.root / 'elsewhere'))
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertIn('people/', res.messages[0].text)

    def test_into_reserved_folder_refused(self) -> None:
        for reserved in ('stubs', 'people/connections'):
            res = person.run_promote(self.root, P_PA, into=reserved)
            self.assertEqual(res.exit_code, EXIT_FAILURE, reserved)
            self.assertIn('reserved', res.messages[0].text)
        self.assertTrue(self.pa_stub.exists())

    def test_into_nested_path_refused(self) -> None:
        res = person.run_promote(self.root, P_PA, into='a/b')
        self.assertEqual(res.exit_code, EXIT_FAILURE)

    def test_into_overrides_destination_for_direct_line(self) -> None:
        res = person.run_promote(self.root, P_PA, into='040 Somewhere Else')
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertTrue(
            (self.root / 'people' / '040 Somewhere Else' / f'line__pa_{P_PA}.md').exists())

    def test_into_does_not_bypass_the_direct_line_rule(self) -> None:
        res = person.run_promote(self.root, P_FRIEND, into='040 Somewhere Else')
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertEqual(res.data['status'], 'refused')

    def test_finishes_a_half_promotion(self) -> None:
        # tier: curated by hand but never moved out of stubs/ - the state the
        # views stub guard refuses. promote finishes the job instead of no-op.
        self.pa_stub.write_text(
            _promote_person_text(P_PA, 'Pa Line', 'M', tier='curated'),
            encoding='utf-8')
        self._reindex()
        res = person.run_promote(self.root, P_PA)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertEqual(res.data['status'], 'ok')
        self.assertFalse(self.pa_stub.exists())
        self.assertTrue(
            (self.root / 'people' / PROMOTE_FOLDER / f'line__pa_{P_PA}.md').exists())

    def test_promote_in_place_when_already_in_a_couple_folder(self) -> None:
        # A stub-tier record already filed in a couple folder: flip + scaffold
        # beside it, no move (folder disagreements stay W110's job).
        misfiled = self.root / 'people' / PROMOTE_FOLDER / f'line__ma_{P_MA}.md'
        (self.root / 'people' / 'stubs' / f'line__ma_{P_MA}.md').rename(misfiled)
        self._reindex()
        res = person.run_promote(self.root, P_MA)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertTrue(misfiled.exists())
        self.assertEqual(str(read_record(misfiled)['meta'].get('tier')), 'curated')
        self.assertTrue(
            (self.root / 'people' / PROMOTE_FOLDER / f'line__ma_research_{P_MA}.md').exists())

    def test_ambiguous_couple_folder_refused_names_both(self) -> None:
        # Two folders share prefix 002 (a hand-organization mistake). Promotion
        # must refuse rather than file the record into an arbitrary half of the
        # couple, and the message names BOTH folders and the rename fix.
        (self.root / 'people' / '002 Someone Else').mkdir()
        res = person.run_promote(self.root, P_PA)
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertEqual(res.data['status'], 'refused')
        msg = res.messages[-1].text
        self.assertIn(PROMOTE_FOLDER, msg)
        self.assertIn('002 Someone Else', msg)
        self.assertIn('rename', msg.lower())
        # Nothing moved - the stub is untouched.
        self.assertTrue(self.pa_stub.exists())

    def test_existing_companion_beside_stub_is_moved_not_duplicated(self) -> None:
        # A hand-written companion sits beside the stub. Promotion must MOVE it
        # to the destination (notes travel with the record), not scaffold a
        # blank one and strand the populated one in stubs/.
        companion = self.root / 'people' / 'stubs' / f'line__pa_research_{P_PA}.md'
        companion.write_text('MY HAND NOTES', encoding='utf-8')
        self._reindex()   # the new companion file makes the prior index stale
        res = person.run_promote(self.root, P_PA)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        moved = self.root / 'people' / PROMOTE_FOLDER / f'line__pa_research_{P_PA}.md'
        self.assertTrue(moved.exists())
        self.assertEqual(moved.read_text(encoding='utf-8'), 'MY HAND NOTES')
        self.assertFalse(companion.exists(), 'stub companion must be vacated')
        comps = list((self.root / 'people').rglob('*_research_*.md'))
        self.assertEqual(len(comps), 1, f'companion duplicated: {comps}')
        all_text = ' '.join(m.text for m in res.messages)
        self.assertIn('Moved the research companion', all_text)

    def _index_update_fails(self):
        """Make the in-place index update raise, and nothing else.

        The bug under test is a cache update that fires AFTER the record has
        already moved. We must fail ONLY that (a locked or read-only
        .cache/index.sqlite), not the moves the promotion itself performs, so
        the archive mutation still reaches disk and we exercise the exact
        post-mutation failure window.
        """
        return mock.patch.object(
            person, 'relocate_person_in_index',
            side_effect=sqlite3.OperationalError('database is locked'))

    def test_index_update_failure_after_move_warns_without_traceback(self) -> None:
        # The record moves, then the in-place index update fails. The
        # promotion already succeeded on disk and cannot be rolled back, so
        # the result is a NON-ZERO warning (not a hard refusal) that names
        # the cache path and the rebuild command - never a leaked traceback.
        stale_cache = self.root / '.cache' / 'index.sqlite'
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self._index_update_fails():
                res = person.run_promote(self.root, P_PA)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertEqual(res.data['status'], 'ok-index-stale')
        # The promotion itself landed: the record left stubs/ for the folder.
        self.assertFalse(self.pa_stub.exists())
        self.assertTrue(
            (self.root / 'people' / PROMOTE_FOLDER / f'line__pa_{P_PA}.md').exists())
        # The now-wrong cache is still on disk - the message must own that
        # fact and point at the fix.
        self.assertTrue(stale_cache.exists())
        msg = res.messages[-1].text
        self.assertIn('index.sqlite', msg)
        self.assertIn('promoted', msg)
        self.assertIn('fha index', msg)
        self.assertEqual(res.messages[-1].next_step, 'fha index')
        # No traceback leaked to stderr.
        self.assertNotIn('Traceback', err.getvalue())

    def test_index_update_failure_on_half_promotion_still_warns(self) -> None:
        # The dangerous half: tier already curated by hand, so the move
        # preserves the record mtime and an un-updated index can pass the
        # freshness check while holding the OLD path. If the update fails
        # here, silence would be worst - assert we still warn.
        self.pa_stub.write_text(
            _promote_person_text(P_PA, 'Pa Line', 'M', tier='curated'),
            encoding='utf-8')
        self._reindex()
        stale_cache = self.root / '.cache' / 'index.sqlite'
        with contextlib.redirect_stderr(io.StringIO()):
            with self._index_update_fails():
                res = person.run_promote(self.root, P_PA)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertEqual(res.data['status'], 'ok-index-stale')
        self.assertFalse(self.pa_stub.exists())
        self.assertTrue(
            (self.root / 'people' / PROMOTE_FOLDER / f'line__pa_{P_PA}.md').exists())
        self.assertTrue(stale_cache.exists())
        self.assertIn('index.sqlite', res.messages[-1].text)

    def test_cli_dry_run_via_fha(self) -> None:
        import fha
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = fha.main(['person', 'promote', P_PA, '--dry-run',
                           '--root', str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn('[dry-run]', out.getvalue())
        self.assertTrue(self.pa_stub.exists())


class SkillInventoryDocsTests(unittest.TestCase):
    """The docs' skill inventory must match what `.claude/skills/` actually holds.

    AGENTS_TOOLING.md §7's status sweep keeps failing in the same direction: a
    skill ships, the BUILD doc that owns it is updated, and the summaries that
    cross-reference it keep quoting the old count and the old list (PR #42 round
    2 - `TOOLING.md` §16 still said thirteen after fifteen had shipped). Prose
    cannot be linted, but a count and a list of folder names can, so this pins
    both against the directory itself.

    It lives in this file only because the round-2 fix was scoped to
    `tests/test_person.py` and `tests/test_packet.py`; it belongs in a docs test
    module of its own the day one exists.
    """

    # Enough of the range to cover a wave or two in either direction. A count
    # word outside this map means the docs and the directory have drifted so
    # far that a person should look, which is what the KeyError says.
    NUMBER_WORDS = {
        11: 'eleven', 12: 'twelve', 13: 'thirteen', 14: 'fourteen', 15: 'fifteen',
        16: 'sixteen', 17: 'seventeen', 18: 'eighteen', 19: 'nineteen', 20: 'twenty',
    }

    # doc -> the phrase whose number word states the count, as a regex with the
    # word captured. Each is a shipped summary an archive owner or a builder
    # reads as the inventory.
    COUNT_PHRASES = {
        'AGENTS.md': r'(\w+) workflow playbooks live at',
        'TOOLING.md': r'all (\w+) authored and shipped',
        'TOOLING_INTERFACE.md': r'all (\w+) SKILL\.md files',
        'BUILD_INTERFACE.md': r'and (\w+) SKILL\.md files',
        # The authoring contract states the count in its own opening sentence
        # ("so N skills written by different sessions ... don't drift"), and it
        # is the first file a new skill's author reads - a stale number there
        # teaches the drift this class exists to catch.
        '.claude/skills/_STANDARD.md': r'so (\w+) skills',
    }

    # doc -> True when it also spells the inventory out name by name.
    # CLAUDE.md is here because it is the harness's own pointer at the skills
    # ("Workflow skills live in .claude/skills/ - process-source, ..."): a skill
    # missing from that line is a skill Claude Code is never told it has.
    NAME_LISTS = ('TOOLING.md', 'BUILD_INTERFACE.md', '.claude/skills/README.md',
                  'CLAUDE.md')

    def setUp(self) -> None:
        self.skills_dir = ROOT / '.claude' / 'skills'
        if not self.skills_dir.is_dir():
            self.skipTest('no .claude/skills/ here (installed archives carry no BUILD docs either)')
        self.skills = sorted(
            d.name for d in self.skills_dir.iterdir()
            if d.is_dir() and (d / 'SKILL.md').is_file()
        )

    def test_every_stated_count_matches_the_directory(self) -> None:
        expected = self.NUMBER_WORDS[len(self.skills)]
        for doc, pattern in self.COUNT_PHRASES.items():
            with self.subTest(doc=doc):
                text = (ROOT / doc).read_text(encoding='utf-8')
                found = re.search(pattern, text)
                self.assertIsNotNone(
                    found, f'{doc} no longer states the skill count in the expected words')
                self.assertEqual(
                    found.group(1).lower(), expected,
                    f'{doc} says {found.group(1)!r} but .claude/skills/ holds '
                    f'{len(self.skills)} skills')

    def test_every_shipped_skill_is_named_where_the_inventory_is_listed(self) -> None:
        for doc in self.NAME_LISTS:
            text = (ROOT / doc).read_text(encoding='utf-8')
            for name in self.skills:
                with self.subTest(doc=doc, skill=name):
                    self.assertIn(name, text, f'{doc} never names the {name} skill')


if __name__ == '__main__':
    unittest.main()
