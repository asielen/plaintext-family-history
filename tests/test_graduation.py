"""
test_graduation.py - the by-hand → tools graduation path, end to end.

The quickstart kit teaches a fully by-hand archive: no IDs anywhere, claims
written with name links (`persons: ["[[Sam Rivera]]"]`), files named after
people ("Sam Rivera.md"). The promised on-ramp to the tools is ONE command -
`fha lint --fix-ids` - after which the archive must actually work: no E-codes,
names resolving to the minted P-ids, claims joining to people in the index.

This file is the flagship proof of that promise, exercised on a copy of the
real `quickstart-example/` fixture shipped in the repo (never the fixture
itself). It also covers `fha stubs`' side of the story: lint E005 tells the
human "create a stub with `fha stubs`", so stubs must see the same wrapped
`[[P-…]]` references lint saw - previously the bracket wrapper made it skip
them, and the advertised fix silently did nothing.
"""

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index
import lint
import stubs
from _lib import EXIT_WARNINGS, build_alias_map, read_record

_QUICKSTART = ROOT / 'quickstart-example'


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


class QuickstartGraduationTests(unittest.TestCase):
    """Copy quickstart-example to a tempdir, graduate it with one
    `lint --fix-ids` run, and prove the result is a working tool archive."""

    @classmethod
    def setUpClass(cls) -> None:
        # One copy + one graduation for the whole class: the fix pass mints
        # random IDs and renames files, so the assertions all read one
        # deterministic post-state rather than re-graduating per test.
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / 'archive'
        shutil.copytree(_QUICKSTART, cls.root)
        cls.fix_result = lint.run_lint(cls.root, {}, fix_ids=True)
        cls.after_result = lint.run_lint(cls.root, {})

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_after_graduation_lint_has_no_e_codes(self) -> None:
        # THE flagship assertion: one --fix-ids run, then a clean-enough lint -
        # warnings allowed (Mills-field nudges etc.), errors not.
        e_msgs = [m for m in self.after_result.messages
                  if (m.code or '').startswith('E')]
        self.assertEqual(e_msgs, [], [f'{m.code}: {m.text}' for m in e_msgs])
        self.assertLessEqual(self.after_result.exit_code, EXIT_WARNINGS)
        self.assertEqual(self.after_result.data['n_errors'], 0)

    def test_nothing_left_on_the_mintable_worklist(self) -> None:
        self.assertEqual(self.after_result.data['mintable'], [])

    def test_records_renamed_to_spec_grammar(self) -> None:
        people = [p.name for p in self.root.rglob('rivera__samuel_P-*.md')]
        self.assertEqual(len(people), 1, people)
        sources = [p.name for p in
                   (self.root / 'sources').glob('sam-rivera-birth-certificate_S-*.md')]
        self.assertEqual(len(sources), 1, sources)

    def test_every_claim_has_a_minted_id(self) -> None:
        for src in (self.root / 'sources').glob('*.md'):
            for claim in read_record(src)['claims']:
                self.assertTrue(
                    str(claim.get('id', '')).lower().startswith('c-'),
                    f'{src.name}: claim without id after graduation: {claim}')

    def test_hand_accepted_claims_carry_reviewed_stamps(self) -> None:
        # The quickstart teaches `status: accepted` with no reviewed: date;
        # the graduation stamps the ones it mints so E006 cannot dead-end it.
        for src in (self.root / 'sources').glob('*.md'):
            for claim in read_record(src)['claims']:
                if str(claim.get('status', '')) == 'accepted':
                    self.assertTrue(str(claim.get('reviewed', '')).strip(),
                                    f'{src.name}: accepted claim missing reviewed:')

    def test_old_name_links_still_resolve(self) -> None:
        # `[[Sam Rivera]]` (the old filename) must keep resolving after the
        # rename - the stem is preserved as an alias, not just its slug.
        recs = []
        for p in list(self.root.rglob('people/**/*.md')) + list((self.root / 'sources').glob('*.md')):
            meta = read_record(p)['meta']
            if meta.get('id'):
                recs.append({'id': meta['id'], 'name': meta.get('name'),
                             'aliases': meta.get('aliases') or []})
        amap = build_alias_map(recs)
        sam_pid = read_record(next(self.root.rglob('rivera__samuel_P-*.md')))['meta']['id']
        self.assertEqual(amap.get('sam rivera'), str(sam_pid).lower())
        self.assertIn('sam rivera birth certificate', amap)

    def test_index_joins_name_linked_claims_to_people(self) -> None:
        # The graduated archive must be QUERYABLE: the birth claim written as
        # persons: ["[[Sam Rivera]]"] joins to Sam's minted P-id, and the
        # relationship claim's roles derive real parent/child edges.
        index.build_index(self.root, {})
        sam_pid = str(read_record(
            next(self.root.rglob('rivera__samuel_P-*.md')))['meta']['id']).lower()
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        try:
            joined = conn.execute(
                '''SELECT COUNT(*) FROM claim_persons cp
                   JOIN claims c ON c.id = cp.claim_id
                   WHERE cp.person_id = ? AND c.type = 'birth' ''',
                (sam_pid,)).fetchone()[0]
            garbage = conn.execute(
                "SELECT COUNT(*) FROM claim_persons WHERE person_id LIKE '[[%'").fetchone()[0]
            parents = conn.execute(
                "SELECT COUNT(*) FROM relationships WHERE person_id = ? AND rel = 'parent'",
                (sam_pid,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(joined, 1)
        self.assertEqual(garbage, 0)
        self.assertEqual(parents, 2)   # Michael and Linda, via resolved roles:


class QuickstartDryRunTests(unittest.TestCase):
    def test_fix_ids_dry_run_leaves_the_copy_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'archive'
            shutil.copytree(_QUICKSTART, root)
            before = {p.relative_to(root): p.read_bytes()
                      for p in root.rglob('*') if p.is_file()}
            result = lint.run_lint(root, {}, fix_ids=True, dry_run=True)
            after = {p.relative_to(root): p.read_bytes()
                     for p in root.rglob('*') if p.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(result.changed, [])
            # The preview names both halves of the work.
            progress = '\n'.join(result.data['progress'])
            self.assertIn('would mint', progress)
            self.assertIn('claim id(s)', progress)


_STUB_SOURCE = '''---
id: S-1111111111
title: Test source
source_type: other
---

## Claims
```yaml
- id: C-1111111111
  type: birth
  persons: ["[[P-9999999999|Ghost Person]]"]
  value: born sometime
  status: suggested
  confidence: low

- id: C-2222222222
  type: note
  persons: ["[[Somebody By Name]]", P-1111111111]
  value: a note
  status: suggested
  confidence: low
```
'''

_STUB_PERSON = '''---
id: P-1111111111
name: Known Person
living: false
---
'''


class StubsWrappedRefTests(unittest.TestCase):
    """`fha stubs` must see the same references lint E005 saw - E005's message
    says "create a stub with `fha stubs`", and that advice was false for
    wrapped `[[P-…]]` refs because the bracket wrapper defeated the
    startswith('p-') test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root / 'sources' / 'test_S-1111111111.md', _STUB_SOURCE)
        _write(self.root / 'people' / 'known__person_P-1111111111.md', _STUB_PERSON)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_wrapped_missing_pid_is_collected(self) -> None:
        unresolved = stubs._collect_unresolved_persons(self.root)
        self.assertIn('p-9999999999', unresolved)

    def test_known_person_and_name_refs_are_not_collected(self) -> None:
        unresolved = stubs._collect_unresolved_persons(self.root)
        self.assertNotIn('p-1111111111', unresolved)   # record exists
        # A NAME is never stubbed from a claim - that is the deliberate
        # `fha stubs --from-names` path (TOOLING §5).
        self.assertEqual(list(unresolved), ['p-9999999999'])

    def test_create_stubs_writes_the_missing_stub(self) -> None:
        created = stubs.create_stubs(self.root, {'p-9999999999': None})
        self.assertEqual(created, 1)
        stub = self.root / 'people' / 'stubs' / 'unknown__unknown_p-9999999999.md'
        self.assertTrue(stub.exists())
        self.assertEqual(
            str(read_record(stub)['meta'].get('id', '')).lower(), 'p-9999999999')

    def test_template_placeholders_are_never_stubbed(self) -> None:
        # `_TEMPLATE.*` teaching files carry placeholder ids like
        # P-xxxxxxxxxx (a VALID Crockford shape); they are not records and
        # must not spawn phantom stubs.
        _write(self.root / 'sources' / '_TEMPLATE.source.md',
               _STUB_SOURCE.replace('P-9999999999', 'P-xxxxxxxxxx')
                           .replace('S-1111111111', 'S-xxxxxxxxxx'))
        unresolved = stubs._collect_unresolved_persons(self.root)
        self.assertNotIn('p-xxxxxxxxxx', unresolved)


if __name__ == '__main__':
    unittest.main()
