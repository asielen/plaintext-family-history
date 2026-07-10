"""
test_person.py - fha person set-living: the surgical living-flag write-back.

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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import person
from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    find_person_record_path,
    load_fha_yaml,
    read_record,
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
        self.assertIn("'true'", text)
        self.assertIn("'false'", text)
        self.assertIn("'unknown'", text)

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


if __name__ == '__main__':
    unittest.main()
