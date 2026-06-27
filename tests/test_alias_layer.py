"""
test_alias_layer.py — the `aliases:` resolution layer (wikilink-native step 03).

Covers the keystone contract: every reference (an ID, a human stem, a person's
name) resolves to one canonical ID through the alias map; the human graph surface
lives in frontmatter (`people: [[Ken Smith]]`); same-name people never silently
resolve; and `fha normalize-links` is the one explicit, previewed rewrite that
settles prose to the canonical form without ever dropping a human stem.
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
import normalize_links as nl
from _lib import alias_clashes, build_alias_map, resolve_ref


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


# ── _lib resolution helpers ───────────────────────────────────────────────────

class ResolveHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            {'id': 'P-de957bcda1', 'name': 'Ken Smith', 'name_variants': ['Kenneth Smith']},
            {'id': 'S-1111111111', 'aliases': ['grandmas-album', 'c-aaaaaaaaaa']},
            {'id': 'L-1111111111', 'name': 'Fairview', 'alt_names': ['Fair View']},
        ]
        self.amap = build_alias_map(self.records)

    def test_resolves_id_stem_name_and_cid(self):
        self.assertEqual(resolve_ref('S-1111111111', self.amap), 's-1111111111')      # ID
        self.assertEqual(resolve_ref('grandmas-album', self.amap), 's-1111111111')     # stem
        self.assertEqual(resolve_ref('[[Ken Smith]]', self.amap), 'p-de957bcda1')      # name
        self.assertEqual(resolve_ref('Kenneth Smith', self.amap), 'p-de957bcda1')      # variant
        self.assertEqual(resolve_ref('c-aaaaaaaaaa', self.amap), 's-1111111111')       # C-id → source
        self.assertEqual(resolve_ref('[[Fairview]]', self.amap), 'l-1111111111')       # place name

    def test_unknown_ref_is_none(self):
        self.assertIsNone(resolve_ref('nobody', self.amap))

    def test_clashing_name_never_resolves(self):
        records = [
            {'id': 'P-aaaaaaaaaa', 'name': 'John Smith'},
            {'id': 'P-bbbbbbbbbb', 'name': 'John Smith'},
        ]
        amap = build_alias_map(records)
        self.assertIsNone(resolve_ref('John Smith', amap))
        self.assertEqual(alias_clashes(records), {'john smith': ['p-aaaaaaaaaa', 'p-bbbbbbbbbb']})
        # The IDs themselves still resolve unambiguously.
        self.assertEqual(resolve_ref('P-aaaaaaaaaa', amap), 'p-aaaaaaaaaa')


# ── index: frontmatter cross-links, on-demand C-ids, stem citations ───────────

class IndexAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root / 'people' / 'smith__ken_P-de957bcda1.md',
               '---\nid: P-de957bcda1\nname: Ken Smith\nliving: false\n---\n## Biography\nx\n')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        return conn

    def test_name_first_people_field_indexes_edge(self):
        _write(self.root / 'sources' / 'a_S-1111111111.md',
               '---\nid: S-1111111111\ntitle: A\nsource_type: photo\n'
               'people: ["[[Ken Smith]]"]\n---\n## Claims\n```yaml\n```\n')
        index.build_index(self.root, {})
        conn = self._conn()
        rows = conn.execute('SELECT person_id FROM source_people').fetchall()
        self.assertEqual([r['person_id'] for r in rows], ['p-de957bcda1'])
        conn.close()

    def test_on_demand_cid_alias_only_when_cited(self):
        body = ('---\nid: S-1111111111\ntitle: A\nsource_type: photo\n---\n'
                '## Claims\n```yaml\n- id: C-aaaaaaaaaa\n  type: residence\n'
                '  persons: [P-de957bcda1]\n  status: accepted\n```\n## Notes\n{cite}\n')
        # Cited: the C-id becomes an alias of its owning source.
        _write(self.root / 'sources' / 'a_S-1111111111.md', body.format(cite='See [[C-aaaaaaaaaa]].'))
        index.build_index(self.root, {})
        conn = self._conn()
        got = conn.execute("SELECT canonical_id FROM aliases WHERE alias='c-aaaaaaaaaa'").fetchone()
        self.assertEqual(got['canonical_id'], 's-1111111111')
        conn.close()
        # Not cited: no C-id alias row.
        _write(self.root / 'sources' / 'a_S-1111111111.md', body.format(cite='No citation here.'))
        index.build_index(self.root, {})
        conn = self._conn()
        self.assertIsNone(conn.execute("SELECT 1 FROM aliases WHERE alias='c-aaaaaaaaaa'").fetchone())
        conn.close()

    def test_stem_citation_records_resolved_canonical_id(self):
        _write(self.root / 'sources' / 'a_S-1111111111.md',
               '---\nid: S-1111111111\naliases: [grandmas-album]\ntitle: A\nsource_type: photo\n---\n'
               '## Notes\nSee [[grandmas-album]].\n')
        index.build_index(self.root, {})
        conn = self._conn()
        tokens = {r['token'] for r in conn.execute("SELECT token FROM citations WHERE kind='S'")}
        self.assertIn('s-1111111111', tokens)   # the stem was recorded as the S-id
        conn.close()


# ── lint: clash detection, self-alias, E004 boundary ──────────────────────────

class LintAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _codes(self):
        findings, _ = lint._run_lint_core(self.root, {})
        return [f.code for f in findings]

    def _two_johns(self):
        _write(self.root / 'people' / 'smith__john1_P-aaaaaaaaaa.md',
               '---\nid: P-aaaaaaaaaa\nname: John Smith\nliving: false\n---\n')
        _write(self.root / 'people' / 'smith__john2_P-bbbbbbbbbb.md',
               '---\nid: P-bbbbbbbbbb\nname: John Smith\nliving: false\n---\n')

    def test_latent_name_clash_is_a_warning(self):
        self._two_johns()
        codes = self._codes()
        self.assertIn('W112', codes)        # latent
        self.assertNotIn('W113', codes)     # not active — nothing links by the name

    def test_active_name_clash_flagged_not_guessed(self):
        self._two_johns()
        _write(self.root / 'sources' / 'a_S-1111111111.md',
               '---\nid: S-1111111111\ntitle: A\nsource_type: photo\n---\n'
               '## Notes\nPictured with [[John Smith]] at the fair.\n')
        codes = self._codes()
        self.assertIn('W113', codes)        # active — a link uses the ambiguous name

    def test_self_alias_warns_only_when_aliases_present_but_wrong(self):
        # No aliases: field → not nagged (forgiving).
        _write(self.root / 'people' / 'doe__jane_P-cccccccccc.md',
               '---\nid: P-cccccccccc\nname: Jane Doe\nliving: false\n---\n')
        self.assertNotIn('W111', self._codes())
        # aliases: present but missing the self-ID → W111.
        _write(self.root / 'people' / 'doe__jane_P-cccccccccc.md',
               '---\nid: P-cccccccccc\naliases: [janey]\nname: Jane Doe\nliving: false\n---\n')
        self.assertIn('W111', self._codes())

    def test_dangling_id_token_is_e004(self):
        _write(self.root / 'sources' / 'a_S-1111111111.md',
               '---\nid: S-1111111111\ntitle: A\nsource_type: photo\n---\n'
               '## Notes\nSee [[S-9999999999]].\n')
        self.assertIn('E004', self._codes())

    def test_unresolved_non_id_stem_is_inert(self):
        # A bare name/stem wikilink that matches no alias is an ordinary Obsidian
        # link, not a citation — no finding at all.
        _write(self.root / 'sources' / 'a_S-1111111111.md',
               '---\nid: S-1111111111\ntitle: A\nsource_type: photo\n---\n'
               '## Notes\nSee [[grandmas-album]].\n')
        codes = self._codes()
        self.assertNotIn('E004', codes)
        self.assertNotIn('E005', codes)

    def test_name_first_people_field_does_not_false_positive(self):
        _write(self.root / 'people' / 'smith__ken_P-de957bcda1.md',
               '---\nid: P-de957bcda1\nname: Ken Smith\nliving: false\n---\n')
        _write(self.root / 'sources' / 'a_S-1111111111.md',
               '---\nid: S-1111111111\ntitle: A\nsource_type: photo\n'
               'people: ["[[Ken Smith]]"]\n---\n## Claims\n```yaml\n```\n')
        self.assertNotIn('E005', self._codes())


# ── normalize-links ───────────────────────────────────────────────────────────

class NormalizeLinksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root / 'people' / 'smith__ken_P-de957bcda1.md',
               '---\nid: P-de957bcda1\naliases: [P-de957bcda1]\nname: Ken Smith\nliving: false\n---\n')
        self.src = self.root / 'sources' / 'a_S-1111111111.md'
        _write(self.src,
               '---\nid: S-1111111111\naliases: [S-1111111111, grandmas-album]\n'
               'title: A\nsource_type: photo\npeople: ["[[Ken Smith]]"]\n---\n'
               '## Claims\n```yaml\n- id: C-aaaaaaaaaa\n  type: residence\n'
               '  persons: [P-de957bcda1]\n  status: accepted\n```\n'
               '## Notes\nVia [S-1111111111], [[grandmas-album]], [[Ken Smith]].\n')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_dry_run_writes_nothing_but_reports(self):
        before = self.src.read_text(encoding='utf-8')
        result = nl.run_normalize_links(self.root, {}, write=False)
        self.assertEqual(self.src.read_text(encoding='utf-8'), before)   # untouched
        self.assertGreater(result.data['edits'], 0)
        self.assertFalse(result.data['written'])
        self.assertTrue(result.data['diffs'])                            # a diff to show

    def test_write_applies_canonical_forms(self):
        nl.run_normalize_links(self.root, {}, write=True)
        text = self.src.read_text(encoding='utf-8')
        self.assertIn('[[S-1111111111]]', text)                          # (a) legacy upgraded
        self.assertIn('[[S-1111111111|grandmas-album]]', text)           # (b) stem, display kept
        self.assertIn('[[P-de957bcda1|Ken Smith]]', text)                # (c) name → ID|name
        self.assertIn('people: ["[[P-de957bcda1|Ken Smith]]"]', text)    # frontmatter settled

    def test_claims_block_bare_ids_never_touched(self):
        nl.run_normalize_links(self.root, {}, write=True)
        text = self.src.read_text(encoding='utf-8')
        self.assertIn('persons: [P-de957bcda1]', text)                   # still bare

    def test_human_stem_is_never_dropped(self):
        nl.run_normalize_links(self.root, {}, write=True)
        text = self.src.read_text(encoding='utf-8')
        self.assertIn('grandmas-album', text)                            # alias survives

    def test_ambiguous_name_left_unchanged_and_flagged(self):
        _write(self.root / 'people' / 'a__john_P-aaaaaaaaaa.md',
               '---\nid: P-aaaaaaaaaa\nname: John Smith\nliving: false\n---\n')
        _write(self.root / 'people' / 'b__john_P-bbbbbbbbbb.md',
               '---\nid: P-bbbbbbbbbb\nname: John Smith\nliving: false\n---\n')
        _write(self.src,
               '---\nid: S-1111111111\naliases: [S-1111111111]\ntitle: A\nsource_type: photo\n---\n'
               '## Notes\nWith [[John Smith]].\n')
        result = nl.run_normalize_links(self.root, {}, write=True)
        self.assertIn('[[John Smith]]', self.src.read_text(encoding='utf-8'))   # never guessed
        self.assertEqual(result.exit_code, 1)                                   # warned
        self.assertTrue(any('ambiguous' in m.text for m in result.messages))


# ── record creation emits aliases: ───────────────────────────────────────────

class RecordCreationAliasTests(unittest.TestCase):
    def test_stub_content_carries_canonical_id_alias(self):
        import stubs
        text = stubs._stub_content('P-de957bcda1', 'Ken Smith')
        self.assertIn('aliases: [P-de957bcda1]', text)

    def test_scaffold_emits_self_alias(self):
        import process
        text = process._scaffold_text(
            'S-1111111111', 'Census page', 'census', [], notes_body=None,
        )
        self.assertIn('aliases: [S-1111111111]', text)

    def test_scaffold_preserves_human_stem(self):
        import process
        text = process._scaffold_text(
            'S-1111111111', 'Census page', 'census', [], notes_body=None,
            stem='grandmas-album',
        )
        self.assertIn('aliases: [S-1111111111, grandmas-album]', text)


if __name__ == '__main__':
    unittest.main()
