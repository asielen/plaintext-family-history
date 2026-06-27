"""
test_provisional_vitals.py — forgiving the hand-author (wikilink-native step 04).

Three forgiving-input behaviours, none of which ever blocks:
  - provisional `birth:`/`death:` fields → an informational needs-sourcing
    backlog (not a warning/error), cleared once an accepted claim supersedes them;
  - claims typed under `## Claims` without the ```yaml fence are read anyway
    (never silently lost) and a warning offers to wrap them;
  - a hand-authored id-less record is auto-mintable (not E002/E010) and
    `--fix-ids` completes it, keeping the filename slug as an alias.
"""

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
from _lib import build_alias_map, read_record, resolve_ref


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


class _LintBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, **kw):
        return lint.run_lint(self.root, {}, **kw)

    def _backlog(self):
        _f, reg = lint._run_lint_core(self.root, {})
        return lint._needs_sourcing_backlog(reg)


# ── Provisional vitals ────────────────────────────────────────────────────────

class ProvisionalVitalsTests(_LintBase):
    def _grandpa(self, extra='', claim=''):
        _write(self.root / 'people' / 'g__grandpa_P-aaaaaaaaaa.md',
               f'---\nid: P-aaaaaaaaaa\nname: Grandpa\nliving: false\n{extra}---\n## Biography\nStory.\n')
        if claim:
            _write(self.root / 'sources' / 's_S-1111111111.md',
                   '---\nid: S-1111111111\ntitle: Bible\nsource_type: artifact\n---\n'
                   '## Claims\n```yaml\n' + claim + '```\n')

    def test_provisional_birth_appears_in_backlog_once(self):
        self._grandpa(extra='birth: 1923\n')
        backlog = self._backlog()
        birth_lines = [b for b in backlog if 'provisional birth' in b]
        self.assertEqual(len(birth_lines), 1)

    def test_backlog_is_informational_not_warning_or_error(self):
        # Adding a provisional field must not change the exit code.
        self._grandpa()
        before = self._run().exit_code
        self._grandpa(extra='birth: 1923\ndeath: 2001\n')
        after = self._run()
        self.assertEqual(after.exit_code, before)
        self.assertTrue(after.data['backlog'])     # but it IS surfaced

    def test_accepted_claim_supersedes_and_clears_backlog(self):
        self._grandpa(
            extra='birth: 1923\n',
            claim='- id: C-aaaaaaaaaa\n  type: birth\n  persons: [P-aaaaaaaaaa]\n'
                  '  value: born 1923\n  status: accepted\n',
        )
        backlog = self._backlog()
        self.assertFalse([b for b in backlog if 'provisional birth' in b])

    def test_suggested_claim_does_not_supersede(self):
        self._grandpa(
            extra='birth: 1923\n',
            claim='- id: C-aaaaaaaaaa\n  type: birth\n  persons: [P-aaaaaaaaaa]\n'
                  '  value: born 1923\n  status: suggested\n',
        )
        self.assertTrue([b for b in self._backlog() if 'provisional birth' in b])

    def test_todo_import_source_prose_tracked(self):
        self._grandpa(extra='')
        _write(self.root / 'people' / 'g__grandpa_P-aaaaaaaaaa.md',
               '---\nid: P-aaaaaaaaaa\nname: Grandpa\nliving: false\n---\n'
               '## Biography\nFarmed near town. (TODO: import source)\n')
        self.assertTrue([b for b in self._backlog() if 'TODO: import source' in b])

    def test_provisional_field_does_not_change_vitals_completeness(self):
        # Whatever W101 (vitals-gap) does for a curated person, a provisional
        # birth: must not satisfy it (a provisional date is not an accepted claim).
        _write(self.root / 'people' / 'a__person_P-bbbbbbbbbb.md',
               '---\nid: P-bbbbbbbbbb\nname: A Person\nliving: false\ntier: curated\n---\n# A Person\n')
        before = [f.code for f in lint._run_lint_core(self.root, {})[0] if f.code == 'W101']
        _write(self.root / 'people' / 'a__person_P-bbbbbbbbbb.md',
               '---\nid: P-bbbbbbbbbb\nname: A Person\nliving: false\ntier: curated\nbirth: 1900\ndeath: 1970\n---\n# A Person\n')
        after = [f.code for f in lint._run_lint_core(self.root, {})[0] if f.code == 'W101']
        self.assertEqual(after, before)

    def test_index_surfaces_provisional_fields(self):
        self._grandpa(extra='birth: 1923\ndeath: 2001\n')
        index.build_index(self.root, {})
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        row = conn.execute("SELECT birth, death FROM persons WHERE id='p-aaaaaaaaaa'").fetchone()
        conn.close()
        self.assertEqual(row, ('1923', '2001'))


class StubScaffoldTests(unittest.TestCase):
    def test_stub_carries_commented_vitals(self):
        text = stubs._stub_content('P-aaaaaaaaaa', 'Jane Doe')
        self.assertIn('# birth:', text)
        self.assertIn('# death:', text)
        # Commented, so they are NOT parsed as real fields.
        self.assertNotIn('birth', read_record_meta(text))


def read_record_meta(frontmatter_text: str) -> dict:
    import tempfile, os
    fd, p = tempfile.mkstemp(suffix='.md')
    os.close(fd)
    Path(p).write_text(frontmatter_text + '\n## Biography\nx\n', encoding='utf-8')
    try:
        return read_record(p)['meta']
    finally:
        os.unlink(p)


# ── Unfenced claims ───────────────────────────────────────────────────────────

class UnfencedClaimsTests(_LintBase):
    def _source(self, claims_section: str):
        _write(self.root / 'sources' / 'a_S-1111111111.md',
               '---\nid: S-1111111111\ntitle: A\nsource_type: census\n---\n'
               f'## Claims\n{claims_section}\n## Notes\nx\n')

    def test_unfenced_claims_indexed(self):
        self._source('- id: C-aaaaaaaaaa\n  type: birth\n  persons: [P-aaaaaaaaaa]\n  value: born 1880\n  status: suggested\n')
        index.build_index(self.root, {})
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        n = conn.execute("SELECT COUNT(*) FROM claims WHERE id='c-aaaaaaaaaa'").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_unfenced_claims_warns_W114(self):
        self._source('- id: C-aaaaaaaaaa\n  type: birth\n  persons: [P-aaaaaaaaaa]\n  value: x\n  status: suggested\n')
        codes = [f.code for f in lint._run_lint_core(self.root, {})[0]]
        self.assertIn('W114', codes)

    def test_fix_claims_fence_dry_run_then_write(self):
        self._source('- id: C-aaaaaaaaaa\n  type: birth\n  persons: [P-aaaaaaaaaa]\n  value: x\n  status: suggested\n')
        src = self.root / 'sources' / 'a_S-1111111111.md'
        before = src.read_text(encoding='utf-8')
        self._run(fix_claims_fence=True, dry_run=True)
        self.assertEqual(src.read_text(encoding='utf-8'), before)    # dry-run writes nothing
        self._run(fix_claims_fence=True, dry_run=False)
        after = src.read_text(encoding='utf-8')
        self.assertIn('```yaml', after)
        self.assertFalse(read_record(src)['unfenced_claims'])        # now fenced
        self.assertEqual(len(read_record(src)['claims']), 1)

    def test_prose_under_claims_not_misread(self):
        self._source('Nothing structured yet — will add claims later.')
        rec = read_record(self.root / 'sources' / 'a_S-1111111111.md')
        self.assertFalse(rec['unfenced_claims'])
        self.assertEqual(rec['claims'], [])
        self.assertNotIn('W114', [f.code for f in lint._run_lint_core(self.root, {})[0]])


# ── Id-less hand-authored records ─────────────────────────────────────────────

class IdlessRecordTests(_LintBase):
    def setUp(self) -> None:
        super().setUp()
        _write(self.root / 'sources' / 'grandmas-letter.md',
               "---\ntitle: Grandma's letter\nsource_type: letter\n---\n## Notes\nA letter.\n")
        _write(self.root / 'people' / 'john-smith.md',
               '---\nname: John Smith\nliving: false\n---\n## Biography\nKnew [[grandmas-letter]].\n')

    def test_idless_is_auto_mintable_not_error(self):
        findings, reg = lint._run_lint_core(self.root, {})
        self.assertEqual(
            sorted(p.name for p, _ in reg.idless_records),
            ['grandmas-letter.md', 'john-smith.md'],
        )
        # Not flagged E002 (filename grammar) or E010 (missing id) for these.
        offenders = [f for f in findings if f.code in ('E002', 'E010')
                     and ('john-smith' in f.path or 'grandmas-letter' in f.path)]
        self.assertEqual(offenders, [])

    def test_bare_lint_does_not_error_on_idless(self):
        result = self._run()
        # Reported as auto-mintable (a worklist), and no error-level exit from it.
        self.assertTrue(result.data['mintable'])
        # The id-less records contribute no E-findings.
        self.assertFalse(any(m.code in ('E002',) and ('john-smith' in (m.path or '')
                         or 'grandmas-letter' in (m.path or '')) for m in result.messages))

    def test_fix_ids_mints_renames_and_keeps_slug_alias(self):
        self._run(fix_ids=True, dry_run=False)
        people = sorted(p.name for p in (self.root / 'people').glob('*.md'))
        sources = sorted(p.name for p in (self.root / 'sources').glob('*.md'))
        self.assertTrue(any(n.startswith('smith__john_P-') for n in people), people)
        self.assertTrue(any(n.startswith('grandmas-letter_S-') for n in sources), sources)
        # The slugs survive as aliases, so the old [[grandmas-letter]] link resolves.
        recs = []
        for p in list((self.root / 'people').glob('*.md')) + list((self.root / 'sources').glob('*.md')):
            m = read_record(p)['meta']
            recs.append({'id': m.get('id'), 'name': m.get('name'), 'aliases': m.get('aliases') or []})
        amap = build_alias_map(recs)
        self.assertIsNotNone(resolve_ref('grandmas-letter', amap))
        self.assertIsNotNone(resolve_ref('john-smith', amap))

    def test_fix_ids_dry_run_changes_nothing(self):
        self._run(fix_ids=True, dry_run=True)
        self.assertTrue((self.root / 'people' / 'john-smith.md').exists())
        self.assertTrue((self.root / 'sources' / 'grandmas-letter.md').exists())


if __name__ == '__main__':
    unittest.main()
