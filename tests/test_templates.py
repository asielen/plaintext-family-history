"""
test_templates.py - the copy-paste template suite (wikilink-native step 05).

The `archive-template/` templates make the "usable by hand, no software"
path real (SPEC §5.2). These tests prove they are spec-valid, not just
illustrative: a filled-in copy of each passes `fha lint` with no errors; the
templates themselves are skipped by every record walk; and they teach the new
forms (manual mint, `aliases:`, `[[ ]]`, provisional vitals) without drifting
from the scaffold output of `fha process` / `fha stubs` / `fha person new`.

`_TEMPLATE.stub.md` is RETIRED (#76): a stub and a curated person are one
shape now (SPEC §16), so there is no separate, smaller "stub" template to
fill in - it is a redirect note pointing at `_TEMPLATE.person.md` (which
already defaults to `tier: stub`). `TemplatesAreSkippedTests` still installs
it and checks it is invisible to lint/index (it is still a real shipped
file, still skipped by `is_template_file`'s filename-only test), but there
is no `FilledTemplatesLintTests` case for it any more, and
`ScaffoldParityTests` compares the stub renderer against `_TEMPLATE.person.md`
instead of the retired file.

`ScaffoldParityTests` also checks the NEW #75/#76 person-body renderer
(`_lib.render_person_body_scaffold` / `ensure_person_body_sections`) the
same way: not by reading `_TEMPLATE.person.md` at runtime (the renderer is
pure Python, kept in step with `render_stub_content`'s existing precedent),
but by asserting the renderer's section HEADINGS match what the template
itself declares - so an edit to one can never silently drift from the other.
"""

import re
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
from _lib import (
    EXIT_ERRORS,
    PERSON_BODY_SECTIONS_TEXT,
    RESEARCH_TEMPLATE_FALLBACK,
    ensure_person_body_sections,
    is_template_file,
    mint_ids,
    read_record,
    render_person_body_scaffold,
    render_stub_content,
)


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
    def test_stub_frontmatter_matches_person_template(self):
        # #76: _TEMPLATE.person.md is the one template for a person at
        # either tier (it already defaults to tier: stub) - compare the
        # stub scaffold's frontmatter against IT now, not the retired
        # _TEMPLATE.stub.md. sex: M is demonstrated in the template as a
        # common-case example; pass it here too so the two field sets match
        # exactly rather than one being a subset of the other.
        tmpl = read_record(TEMPLATES / 'people' / '_TEMPLATE.person.md')['meta']
        import os
        fd, p = tempfile.mkstemp(suffix='.md'); os.close(fd)
        Path(p).write_text(
            render_stub_content('P-de957bcda1', 'Jane Doe', sex='M'), encoding='utf-8')
        try:
            scaffold = read_record(p)['meta']
        finally:
            os.unlink(p)
        self.assertEqual(sorted(tmpl.keys()), sorted(scaffold.keys()))

    def test_person_body_scaffold_headings_match_the_template(self):
        # The coordinator's cross-check: the #75/#76 body renderer (used by
        # `fha person new`/`fha stubs`) is pure Python, not a runtime read of
        # the template - so THIS test is what keeps the two from silently
        # drifting apart, the same discipline the frontmatter checks above
        # already apply to render_stub_content/_scaffold_text.
        tmpl_text = (TEMPLATES / 'people' / '_TEMPLATE.person.md').read_text(encoding='utf-8')
        tmpl_headings = set(re.findall(r'^## (.+)$', tmpl_text, re.M))
        rendered = render_person_body_scaffold('Jane Doe')
        rendered_headings = set(re.findall(r'^## (.+)$', rendered, re.M))
        self.assertEqual(rendered_headings, tmpl_headings)
        # And the purpose block's own signature line is there too - the one
        # piece of #75's shape that isn't a `## ` heading.
        self.assertIn("record - yours to write", tmpl_text)
        self.assertIn("record - yours to write", rendered)

    def test_person_body_sections_text_is_the_template_verbatim(self):
        # Owner simplification (2026-08): the four hand-written sections
        # never vary per person, so PERSON_BODY_SECTIONS_TEXT isn't rendered
        # at all - it IS the template's `## Biography`-onward text, byte for
        # byte. Unlike the heading-set check above, this catches wording
        # drift too (a hand-typed placeholder could previously diverge from
        # the template without any test noticing - only its HEADING was
        # checked). Slicing at test time, never at scaffold time (_lib does
        # not read the template file at runtime - see the coordinator's
        # render_stub_content precedent).
        tmpl_text = (TEMPLATES / 'people' / '_TEMPLATE.person.md').read_text(encoding='utf-8')
        tmpl_tail = tmpl_text[tmpl_text.index('## Biography'):]
        self.assertEqual(PERSON_BODY_SECTIONS_TEXT, tmpl_tail)

    def test_ensure_person_body_sections_from_empty_matches_the_scaffold(self):
        # `ensure_person_body_sections` (the ADDITIVE promote-time backfill)
        # and `render_person_body_scaffold` (the brand-new-record renderer)
        # must agree on what "complete" looks like: starting from an empty
        # body, backfilling everything produces the same heading set as
        # rendering it fresh.
        empty_record = '---\nid: P-de957bcda1\nname: Jane Doe\n---\n'
        backfilled, added = ensure_person_body_sections(empty_record, 'Jane Doe')
        self.assertEqual(
            added, ['title', 'purpose block', 'Sources', 'Biography',
                    'Stories', 'Research Notes', 'Friends & Family'])
        backfilled_headings = set(re.findall(r'^## (.+)$', backfilled, re.M))
        scaffold_headings = set(re.findall(
            r'^## (.+)$', render_person_body_scaffold('Jane Doe'), re.M))
        self.assertEqual(backfilled_headings, scaffold_headings)

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

    def test_research_fallback_matches_the_template(self):
        # #75 names the `_research` companion explicitly in scope alongside
        # person/source records, so its purpose block was added to BOTH
        # _TEMPLATE.research.md and RESEARCH_TEMPLATE_FALLBACK (the built-in
        # scaffold `render_research_content` falls back to for an archive
        # that predates the template file) - this is what keeps the two from
        # silently drifting apart, the same discipline every other
        # renderer/template pair in this file already keeps.
        tmpl_text = (TEMPLATES / 'people' / '_TEMPLATE.research.md').read_text(encoding='utf-8')
        tmpl_headings = re.findall(r'^## (.+)$', tmpl_text, re.M)
        fallback_headings = re.findall(r'^## (.+)$', RESEARCH_TEMPLATE_FALLBACK, re.M)
        self.assertEqual(fallback_headings, tmpl_headings)
        self.assertIn("research workspace - yours to write", tmpl_text)
        self.assertIn("research workspace - yours to write", RESEARCH_TEMPLATE_FALLBACK)


class TemplateHygieneTests(unittest.TestCase):
    def test_no_generated_header(self):
        for rel in ('sources/_TEMPLATE.source.md', 'people/_TEMPLATE.person.md',
                    'people/stubs/_TEMPLATE.stub.md', 'notes/questions.md'):
            text = (TEMPLATES / rel).read_text(encoding='utf-8')
            self.assertNotIn('GENERATED', text, rel)


if __name__ == '__main__':
    unittest.main()
