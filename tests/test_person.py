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

Fixtures only (AGENTS_TOOLING §5): everything runs against temp trees or a
copy of example-archive; the real archive is never touched.
"""

import contextlib
import io
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
        # `_lib.stub_filename` helper rather than inventing its own mononym
        # logic (task contract): a single-word name currently slugs to an
        # 'unknown' surname component (`_lib.stub_slug_name`), giving
        # unknown__cher_{P-id}.md - not the SPEC §13 leading-double-underscore
        # form a hand-filed mononym (e.g. __caesar_P-....md) uses. That is
        # existing, out-of-scope `_lib.py` behavior this test pins down.
        self.assertTrue(path.name.startswith('unknown__cher_'))
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
        for verb in ('new', 'set-living', 'relate', 'estimate', 'edit', 'note'):
            self.assertIn(verb, out)


if __name__ == '__main__':
    unittest.main()
