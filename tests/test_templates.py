"""
test_templates.py — the copy-paste template suite (wikilink-native step 05).

The five `archive-template/` templates make the "usable by hand, no software"
path real (SPEC §5.2). These tests prove they are spec-valid, not just
illustrative: a filled-in copy of each passes `fha lint` with no errors; the
templates themselves are skipped by every record walk; and they teach the new
forms (manual mint, `aliases:`, `[[ ]]`, provisional vitals) without drifting
from the scaffold output of `fha process` / `fha stubs`.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
TEMPLATES = ROOT / 'archive-template'

import index
import lint
import process
import stubs
from _lib import EXIT_ERRORS, is_template_file, mint_ids, read_record


def _fill(text: str, **codes: str) -> str:
    """Replace each `X-__________` placeholder with a real minted code."""
    for prefix, code in codes.items():
        text = text.replace(f'{prefix}-__________', code)
    return text


class _ArchiveBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        for sub in ('people', 'people/stubs', 'sources', 'places', 'notes', 'documents'):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _errors(self):
        findings, _ = lint._run_lint_core(self.root, {})
        return [f for f in findings if f.severity == 'E']


class FilledTemplatesLintTests(_ArchiveBase):
    def test_filled_source_template_has_no_errors(self):
        sid, pid, cid = mint_ids('S', 1, self.root)[0], mint_ids('P', 1, self.root)[0], mint_ids('C', 1, self.root)[0]
        text = _fill((TEMPLATES / 'sources' / '_TEMPLATE.source.md').read_text(encoding='utf-8'),
                     S=sid, P=pid, C=cid)
        (self.root / 'sources' / f'filled_{sid}.md').write_text(text, encoding='utf-8')
        # the claim names a real person; the file the source points at must exist
        (self.root / 'people' / f'cole__margaret_{pid}.md').write_text(
            f'---\nid: {pid}\naliases: [{pid}]\nname: Margaret Cole\nliving: false\n---\n# Margaret Cole\n',
            encoding='utf-8')
        (self.root / 'documents' / 'put-your-file-here.jpg').write_bytes(b'x')
        self.assertEqual(self._errors(), [])

    def test_filled_person_template_has_no_errors(self):
        pid, sid = mint_ids('P', 1, self.root)[0], mint_ids('S', 1, self.root)[0]
        text = _fill((TEMPLATES / 'people' / '_TEMPLATE.person.md').read_text(encoding='utf-8'),
                     P=pid, S=sid).replace('Full Name Here', 'Thomas Hartley')
        (self.root / 'people' / f'hartley__thomas_{pid}.md').write_text(text, encoding='utf-8')
        # the summary block cites a source by [[S-…]]; that source must exist
        (self.root / 'sources' / f'src_{sid}.md').write_text(
            f'---\nid: {sid}\naliases: [{sid}]\ntitle: A source\nsource_type: census\n---\n## Claims\n```yaml\n```\n',
            encoding='utf-8')
        self.assertEqual(self._errors(), [])

    def test_filled_stub_template_has_no_errors(self):
        pid = mint_ids('P', 1, self.root)[0]
        text = _fill((TEMPLATES / 'people' / 'stubs' / '_TEMPLATE.stub.md').read_text(encoding='utf-8'),
                     P=pid).replace('Full Name Here', 'Jane Doe')
        (self.root / 'people' / 'stubs' / f'doe__jane_{pid}.md').write_text(text, encoding='utf-8')
        self.assertEqual(self._errors(), [])

    def test_filled_place_template_parses_and_lints(self):
        lid = mint_ids('L', 1, self.root)[0]
        raw = (TEMPLATES / 'places' / 'places.yaml').read_text(encoding='utf-8')
        # Uncomment only the structural example lines (a YAML list item or an
        # indented key), leaving the prose header comments out entirely.
        lines = []
        for ln in raw.splitlines():
            if ln.startswith('# - ') or ln.startswith('#   '):
                lines.append(ln.replace('# ', '', 1))
        text = _fill('\n'.join(lines), L=lid) + '\n'
        (self.root / 'places' / 'places.yaml').write_text(text, encoding='utf-8')
        # It registers a place and produces no lint errors.
        self.assertEqual(self._errors(), [])
        findings, reg = lint._run_lint_core(self.root, {})
        self.assertIn(lid.lower(), reg.place_ids)


class TemplatesAreSkippedTests(_ArchiveBase):
    def _install_templates(self):
        (self.root / 'sources' / '_TEMPLATE.source.md').write_text(
            (TEMPLATES / 'sources' / '_TEMPLATE.source.md').read_text(encoding='utf-8'), encoding='utf-8')
        (self.root / 'people' / '_TEMPLATE.person.md').write_text(
            (TEMPLATES / 'people' / '_TEMPLATE.person.md').read_text(encoding='utf-8'), encoding='utf-8')
        (self.root / 'people' / 'stubs' / '_TEMPLATE.stub.md').write_text(
            (TEMPLATES / 'people' / 'stubs' / '_TEMPLATE.stub.md').read_text(encoding='utf-8'), encoding='utf-8')

    def test_lint_ignores_templates(self):
        self._install_templates()
        # No findings at all reference a _TEMPLATE file (the malformed placeholder
        # id: S-__________ must NOT trip E002).
        findings, _ = lint._run_lint_core(self.root, {})
        self.assertEqual([f for f in findings if '_TEMPLATE' in f.path], [])

    def test_index_ignores_templates(self):
        self._install_templates()
        index.build_index(self.root, {})
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        n_src = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        n_ppl = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        conn.close()
        self.assertEqual((n_src, n_ppl), (0, 0))   # placeholder ids never indexed

    def test_is_template_file_helper(self):
        self.assertTrue(is_template_file('sources/_TEMPLATE.source.md'))
        self.assertFalse(is_template_file('sources/real_S-1234567890.md'))


class ScaffoldParityTests(unittest.TestCase):
    def test_stub_template_fields_match_stub_scaffold(self):
        tmpl = read_record(TEMPLATES / 'people' / 'stubs' / '_TEMPLATE.stub.md')['meta']
        import os
        fd, p = tempfile.mkstemp(suffix='.md'); os.close(fd)
        Path(p).write_text(stubs._stub_content('P-de957bcda1', 'Jane Doe'), encoding='utf-8')
        try:
            scaffold = read_record(p)['meta']
        finally:
            os.unlink(p)
        self.assertEqual(sorted(tmpl.keys()), sorted(scaffold.keys()))

    def test_source_scaffold_fields_present_in_template(self):
        import os
        fd, p = tempfile.mkstemp(suffix='.md'); os.close(fd)
        Path(p).write_text(
            process._scaffold_text('S-1111111111', 'A', 'census', [], notes_body=None),
            encoding='utf-8')
        try:
            scaffold_keys = set(read_record(p)['meta'].keys())
        finally:
            os.unlink(p)
        tmpl_keys = set(read_record(TEMPLATES / 'sources' / '_TEMPLATE.source.md')['meta'].keys())
        # Every field the scaffolder emits is taught by the template (the template
        # may also show optional fields the scaffolder omits, like places).
        missing = scaffold_keys - tmpl_keys - {'source_class'}  # source_class shown as advanced comment
        self.assertEqual(missing, set(), f'template missing scaffold fields: {missing}')


class TemplateHygieneTests(unittest.TestCase):
    def test_no_generated_header(self):
        for rel in ('sources/_TEMPLATE.source.md', 'people/_TEMPLATE.person.md',
                    'people/stubs/_TEMPLATE.stub.md', 'notes/questions.md'):
            text = (TEMPLATES / rel).read_text(encoding='utf-8')
            self.assertNotIn('GENERATED', text, rel)


if __name__ == '__main__':
    unittest.main()
