"""
test_obsidian_pkm.py - chunk 06 (Obsidian/PKM integration).

Covers the two pieces of chunk 06 that carry executable contracts:

  * 06b - the family tree's edges carry their *nature* (genetic vs social) so the
    site renderer can draw an adoptive/step edge distinctly from the genetic line
    (SPEC §12.2). Verified on both backends: `views._collect_edges` (the neutral
    tree JSON contract) and the JSON `fha site` actually writes.
  * 06c - the optional `obsidian-templater/` pack emits spec-valid records: a
    note made from a template (ID left blank for `fha` to mint) lints with no
    errors - it is the valid pre-machine state (SPEC §10/§13), not a violation.

Temp fixtures throughout; never the real archive or example-archive.
"""

import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import importlib.util
import index as index_mod
import views
import lint
from _lib import load_fha_yaml, normalize_id

# `site` shadows Python's stdlib site module (already cached in sys.modules at
# startup), so `import site` finds the wrong one - load tools/site.py by path,
# exactly as fha.py does.
_spec = importlib.util.spec_from_file_location('fha_site', ROOT / 'tools' / 'site.py')
site_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site_mod)

CHILD = 'P-aaaaaaaaaa'
BIO = 'P-bbbbbbbbbb'      # biological father (genetic edge)
ADO = 'P-cccccccccc'      # adoptive father (non-genetic edge)
SID = 'S-dddddddddd'


def _ptext(pid: str, name: str, sex: str, tier: str = 'stub') -> str:
    return (f'---\nid: {pid}\nname: {name}\nsex: {sex}\nliving: false\n'
            f'tier: {tier}\n---\n\n# {name}\n\n## Biography\n\nx\n')


def _rel(cid: str, child: str, parent: str, subtype: str) -> str:
    return (f'- value: "{child} child of {parent}"\n'
            f'  id: {cid}\n  type: relationship\n  subtype: {subtype}\n'
            f'  persons: [{child}, {parent}]\n  roles: {{child: {child}, parent: {parent}}}\n'
            f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
            f'  information: primary\n  evidence: direct\n  notes: x.\n')


def _build_tree_archive() -> Path:
    root = Path(tempfile.mkdtemp())
    (root / 'people' / 'stubs').mkdir(parents=True)
    (root / 'sources' / 'notes').mkdir(parents=True)
    (root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
    (root / 'people' / 'stubs' / f'kid__a_{CHILD}.md').write_text(
        _ptext(CHILD, 'Kid Aye', 'M', tier='curated'), encoding='utf-8')
    (root / 'people' / 'stubs' / f'bio__pa_{BIO}.md').write_text(_ptext(BIO, 'Bio Pa', 'M'), encoding='utf-8')
    (root / 'people' / 'stubs' / f'ado__pa_{ADO}.md').write_text(_ptext(ADO, 'Ado Pa', 'M'), encoding='utf-8')
    claims = (_rel('C-1111111111', CHILD, BIO, 'biological')
              + _rel('C-2222222222', CHILD, ADO, 'adoptive'))
    (root / 'sources' / 'notes' / f'{SID.lower()}.md').write_text(
        f'---\nid: {SID}\ntitle: Family\nsource_type: other\n---\n\n## Claims\n```yaml\n{claims}```\n',
        encoding='utf-8')
    index_mod.build_index(root, load_fha_yaml(root))
    return root


class TreeEdgeNatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _build_tree_archive()

    def test_views_collect_edges_carry_nature(self):
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.row_factory = sqlite3.Row
        try:
            edges = views._collect_edges(conn, CHILD.lower(), ['parent'])
        finally:
            conn.close()
        by_to = {e['to']: e for e in edges}
        self.assertTrue(by_to[BIO.lower()]['genetic'])          # biological -> genetic
        self.assertFalse(by_to[ADO.lower()]['genetic'])         # adoptive   -> not genetic
        self.assertEqual(by_to[ADO.lower()]['subtype'], 'adoptive')

    def test_site_tree_json_carries_nature(self):
        # The neutral tree JSON must tag each parent/child edge with its nature
        # (genetic vs a legal/adoptive bond). The interactive ancestors tree is no
        # longer emitted per page (person pages show a static pedigree), so exercise
        # the builder that produces the JSON directly - the biological and adoptive
        # fathers are co-parents of one child, which no single descendant tree holds.
        conn = site_mod.open_index_db(self.root, site_mod._REQUIRED_TABLES, strict=False)
        try:
            builder = site_mod._SiteBuilder(
                conn, self.root, {}, self.root / '.cache' / 'site', linked=True)
            builder.prepare()
            data = builder._build_tree_data(normalize_id(CHILD), 'ancestors', 2, builder.out_dir)
        finally:
            conn.close()
        natures = {e['to']: e['genetic'] for e in data['edges']}
        self.assertIn('genetic', data['edges'][0])              # contract field present
        self.assertTrue(natures.get('P-bbbbbbbbbb'))           # biological father
        self.assertFalse(natures.get('P-cccccccccc'))          # adoptive father drawn distinctly


# ── 06c: Templater pack emits spec-valid records ────────────────────────────────

def _fill_template(text: str) -> str:
    """Crudely render a Templater template the way Obsidian would: drop the
    leading `<%* … -%>` JS block and substitute the `<% … %>` outputs."""
    text = re.sub(r'<%\*.*?-%>\s*', '', text, flags=re.S)
    text = text.replace('<% name %>', 'Test Person').replace('<% title %>', 'Test Source')
    text = re.sub(r'<% tp\.date\.now\([^)]*\) %>', '2026-01-01', text)
    text = re.sub(r'<%.*?%>', 'x', text)
    return text.lstrip()


class TemplaterPackTests(unittest.TestCase):
    PACK = ROOT / 'obsidian-templater'

    def _lint_one(self, rel: str, text: str):
        root = Path(tempfile.mkdtemp())
        (root / 'people' / 'stubs').mkdir(parents=True)
        (root / 'sources' / 'notes').mkdir(parents=True)
        (root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        (root / rel).write_text(text, encoding='utf-8')
        findings, _ = lint._run_lint_core(root, {})
        return [f for f in findings if f.severity == 'E']

    def test_person_template_lints_without_error(self):
        filled = _fill_template((self.PACK / 'new-person.md').read_text(encoding='utf-8'))
        self.assertIn('name: Test Person', filled)
        # No ID + a hand-named file = the valid pre-machine state; no E-level finding.
        errors = self._lint_one('people/stubs/test-person.md', filled)
        self.assertEqual(errors, [], f'unexpected: {[(f.code, f.message) for f in errors]}')

    def test_source_template_lints_without_error(self):
        filled = _fill_template((self.PACK / 'new-source.md').read_text(encoding='utf-8'))
        self.assertIn('title: Test Source', filled)
        errors = self._lint_one('sources/notes/test-source.md', filled)
        self.assertEqual(errors, [], f'unexpected: {[(f.code, f.message) for f in errors]}')

    def test_graph_config_is_valid_json(self):
        json.loads((self.PACK / 'graph.json').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
