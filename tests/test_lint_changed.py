"""
test_lint_changed.py: `fha lint --changed` and `fha lint --timings`.

A full lint is the check before a release; `--changed` is the quick check
before a commit. It still runs every check over the whole archive, but the
report keeps only the findings on the files changed since a git commit (plus
new files) and on the records those files link to or are linked from. These
tests build a tiny archive in a real git repository, change one thing, and
check which findings the narrowed report keeps:

  - a changed source keeps its own finding and drops an unrelated one;
  - a changed person brings in the source whose claim names them;
  - a deleted person brings in the source still naming them (E005);
  - a new, untracked file is reported;
  - outside git, the whole report comes back with a plain note;
  - a fix flag is refused beside --changed (a fix works on everything);
  - --timings writes its table to stderr only, so --json stays one document.

It also guards the speed of a full lint: the reverse relationships-block
check (W115) reads each claim once, not once per person with a
relationships: block, which is what made a large archive take minutes.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import lint

_GIT = shutil.which('git')


def _person(pid: str, name: str) -> str:
    return (f'---\nid: {pid}\nname: {name}\nliving: false\n---\n\n'
            '## Biography\n\nSome text.\n')


def _source(sid: str, cid: str, pid: str) -> str:
    """A source with one suggested claim naming `pid`: lint reports it as
    W102 (a suggested claim awaiting review), a finding on the source."""
    return (
        f'---\nid: {sid}\ntitle: Test source\nsource_type: other\n---\n\n'
        '## Claims\n\n```yaml\n'
        f'- value: a fact\n  id: {cid}\n  type: residence\n'
        f'  persons: [{pid}]\n  date: 1900\n  confidence: medium\n'
        '  status: suggested\n```\n'
    )


ADA = 'people/stubs/hartley__ada_P-1111111111.md'
RUTH = 'people/stubs/cole__ruth_P-2222222222.md'
SRC_A = 'sources/notes/ada-note_S-aaaaaaaaaa.md'
SRC_B = 'sources/notes/ruth-note_S-bbbbbbbbbb.md'


class _ArchiveBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        files = {
            'fha.yaml': 'root_person: P-1111111111\n',
            ADA: _person('P-1111111111', 'Ada Hartley'),
            RUTH: _person('P-2222222222', 'Ruth Cole'),
            SRC_A: _source('S-aaaaaaaaaa', 'C-aaaaaaaaaa', 'P-1111111111'),
            SRC_B: _source('S-bbbbbbbbbb', 'C-bbbbbbbbbb', 'P-2222222222'),
        }
        for rel, text in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run([_GIT, '-c', 'user.name=Test', '-c', 'user.email=t@example.com',
                        '-c', 'commit.gpgsign=false', *args],
                       cwd=self.root, check=True, capture_output=True)

    def _commit_all(self) -> None:
        self._git('init', '-q')
        self._git('add', '-A')
        self._git('commit', '-q', '-m', 'start')

    def _lint(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = lint._standalone_main(['--root', str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def _w102_paths(self, stdout: str) -> set[str]:
        return {line.split(' ', 2)[2].split(':', 1)[0].replace('\\', '/')
                for line in stdout.splitlines() if line.startswith('W W102 ')}


@unittest.skipUnless(_GIT, 'git is not installed')
class ChangedScopeTests(_ArchiveBase):
    def test_full_lint_reports_both_sources(self) -> None:
        # The baseline every narrowed run below is compared with.
        self._commit_all()
        _code, out, _err = self._lint()
        self.assertEqual(self._w102_paths(out), {SRC_A, SRC_B})

    def test_nothing_changed_reports_nothing_and_says_so(self) -> None:
        self._commit_all()
        code, out, _err = self._lint('--changed')
        self.assertEqual(self._w102_paths(out), set())
        self.assertIn('No file has changed since HEAD', out)
        self.assertIn('without --changed', out)
        self.assertEqual(code, 0)

    def test_changed_source_keeps_its_finding_only(self) -> None:
        self._commit_all()
        with open(self.root / SRC_A, 'a', encoding='utf-8') as f:
            f.write('\nA note added by hand.\n')
        code, out, _err = self._lint('--changed')
        self.assertEqual(self._w102_paths(out), {SRC_A})
        self.assertEqual(code, 1)

    def test_changed_person_brings_in_the_source_naming_them(self) -> None:
        self._commit_all()
        with open(self.root / ADA, 'a', encoding='utf-8') as f:
            f.write('\nMore about Ada.\n')
        _code, out, _err = self._lint('--changed')
        self.assertEqual(self._w102_paths(out), {SRC_A})

    def test_deleted_person_brings_in_the_source_still_naming_them(self) -> None:
        self._commit_all()
        (self.root / ADA).unlink()
        _code, out, _err = self._lint('--changed')
        e005 = [line for line in out.splitlines() if line.startswith('E E005 ')]
        self.assertTrue(e005)
        self.assertTrue(all('ada-note' in line for line in e005))
        self.assertNotIn('ruth-note', out)

    def test_new_untracked_file_is_reported(self) -> None:
        self._commit_all()
        new = 'sources/notes/new-note_S-cccccccccc.md'
        (self.root / new).write_text(
            _source('S-cccccccccc', 'C-cccccccccc', 'P-2222222222'), encoding='utf-8')
        _code, out, _err = self._lint('--changed')
        self.assertIn(new, self._w102_paths(out))
        # The new source names Ruth, so Ruth's record is in scope; her other
        # source only names Ruth and is not.
        self.assertNotIn(SRC_B, self._w102_paths(out))

    def test_older_ref_widens_the_report(self) -> None:
        self._commit_all()
        with open(self.root / SRC_B, 'a', encoding='utf-8') as f:
            f.write('\nEdited.\n')
        self._git('add', '-A')
        self._git('commit', '-q', '-m', 'second')
        _code, out, _err = self._lint('--changed')
        self.assertEqual(self._w102_paths(out), set())
        _code, out, _err = self._lint('--changed', 'HEAD~1')
        self.assertEqual(self._w102_paths(out), {SRC_B})

    def test_json_stdout_stays_one_document(self) -> None:
        self._commit_all()
        (self.root / SRC_A).write_text(
            (self.root / SRC_A).read_text(encoding='utf-8') + '\nEdit.\n', encoding='utf-8')
        _code, out, err = self._lint('--changed', '--json')
        payload = json.loads(out)
        self.assertEqual({p['code'] for p in payload}, {'W102'})
        self.assertIn('Reporting on the 1 file(s) changed since HEAD', err)

    def test_unknown_ref_checks_everything_and_says_why(self) -> None:
        self._commit_all()
        _code, out, _err = self._lint('--changed', 'no-such-branch')
        self.assertIn("git has no commit called 'no-such-branch'", out)
        self.assertEqual(self._w102_paths(out), {SRC_A, SRC_B})


class ChangedOutsideGitTests(_ArchiveBase):
    def test_not_a_repository_checks_everything_and_says_so(self) -> None:
        # The temporary folder is not inside any git repository.
        code, out, _err = self._lint('--changed')
        if 'not kept in a git repository' not in out and 'not installed' not in out:
            self.skipTest('the temporary folder sits inside a git repository here')
        self.assertIn('checked the whole archive instead', out)
        self.assertEqual(self._w102_paths(out), {SRC_A, SRC_B})
        self.assertEqual(code, 1)

    def test_fix_flag_is_refused_beside_changed(self) -> None:
        code, out, err = self._lint('--changed', '--fix-ids')
        self.assertEqual(code, 2)
        self.assertIn('cannot be used with --fix-ids', err)
        self.assertIn('`fha lint --fix-ids`', err)
        self.assertEqual(out, '')


class InChangedScopeTests(unittest.TestCase):
    """Which finding paths the narrowed report keeps, given the scope."""

    def setUp(self) -> None:
        self.root = Path(tempfile.gettempdir()) / 'archive'
        folder = self.root / 'people' / '040 Thomas Hartley + Ethel Cole'
        self.changed = folder / 'hartley__thomas_P-1111111111.md'
        self.scope = {lint._path_key(self.changed)}
        self.folder = folder

    def test_changed_record_kept_other_record_dropped(self) -> None:
        self.assertTrue(lint._in_changed_scope(self.changed, self.root, self.scope))
        other = self.root / 'sources' / 'notes' / 'x_S-aaaaaaaaaa.md'
        self.assertFalse(lint._in_changed_scope(other, self.root, self.scope))

    def test_folder_holding_a_changed_record_is_kept(self) -> None:
        # A couple folder's bracket-list finding sits on the folder itself.
        self.assertTrue(lint._in_changed_scope(self.folder, self.root, self.scope))
        other = self.root / 'people' / '041 Calvin Hartley + Frances Bradford'
        self.assertFalse(lint._in_changed_scope(other, self.root, self.scope))

    def test_archive_wide_files_always_kept(self) -> None:
        for path in (self.root / 'fha.yaml', self.root / 'places' / 'places.yaml',
                     self.root / 'AGENTS.md'):
            self.assertTrue(lint._in_changed_scope(path, self.root, self.scope))


class ReverseRelationshipCheckReadsClaimsOnceTests(unittest.TestCase):
    """Thirty people with a relationships: block and thirty parent-child
    claims: the reverse W115 check labels each claim for the people it names
    (sixty calls), where walking every claim per person made 900."""

    N = 30

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('', encoding='utf-8')
        people = self.root / 'people' / 'stubs'
        people.mkdir(parents=True)
        pids = [f'P-{n:02d}00000000' for n in range(self.N + 1)]
        for n, pid in enumerate(pids):
            (people / f'hartley__person{n}_{pid}.md').write_text(
                f'---\nid: {pid}\nname: Person {n} Hartley\nliving: false\n'
                f'relationships:\n  - type: parent\n    to: "{pids[0]}"\n---\n',
                encoding='utf-8')
        claims = ''.join(
            f'- value: child of\n  id: C-{n:02d}00000000\n  type: relationship\n'
            f'  persons: [{pids[n]}, {pids[0]}]\n'
            f'  roles:\n    child: {pids[n]}\n    parent: {pids[0]}\n'
            '  confidence: high\n  status: accepted\n'
            for n in range(1, self.N + 1))
        sources = self.root / 'sources' / 'notes'
        sources.mkdir(parents=True)
        (sources / 'family_S-0000000000.md').write_text(
            '---\nid: S-0000000000\ntitle: Family list\nsource_type: other\n---\n\n'
            f'## Claims\n\n```yaml\n{claims}```\n', encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_each_claim_is_labelled_once_per_named_person(self) -> None:
        calls = []
        real = lint._person_reconcilable_role_label

        def counting(claim, pid, alias_map=None):
            calls.append(pid)
            return real(claim, pid, alias_map)

        lint._person_reconcilable_role_label = counting
        try:
            findings, _registry = lint._run_lint_core(self.root, {})
        finally:
            lint._person_reconcilable_role_label = real
        self.assertLessEqual(len(calls), 2 * self.N)
        # The check still reports every claim a block leaves out: each child
        # misses their own claim, and the parent misses all thirty.
        reverse = [f for f in findings if f.code == 'W115'
                   and 'has a relationships: block but accepted claim' in f.message]
        self.assertEqual(len(reverse), 2 * self.N)


class TimingsTests(_ArchiveBase):
    def test_timings_go_to_stderr_once(self) -> None:
        _code, out, err = self._lint('--timings', '--json')
        json.loads(out)   # stdout is still exactly the JSON document
        self.assertEqual(err.count('Seconds per group of checks:'), 1)
        self.assertIn('reading records (one-file checks)', err)
        self.assertIn('relationships blocks', err)
        self.assertRegex(err, r'\d+\.\d\d  total')

    def test_no_timings_without_the_flag(self) -> None:
        _code, _out, err = self._lint()
        self.assertNotIn('Seconds per group', err)


if __name__ == '__main__':
    unittest.main()
