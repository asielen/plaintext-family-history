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
    PERSON_BODY_SECTIONS,
    PERSON_BODY_SECTIONS_TEXT,
    RESEARCH_TEMPLATE_FALLBACK,
    ensure_person_body_sections,
    is_template_file,
    mint_ids,
    person_section_is_unfilled,
    read_record,
    render_person_body_scaffold,
    render_stub_content,
    strip_unfilled_person_sections,
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
    def test_source_type_vocabulary_matches_the_template_comment(self):
        # #114 follow-up (Codex review, PR #178): `ephemera` joined
        # `_lib.SOURCE_TYPES` but the shipped `_TEMPLATE.source.md`'s "one
        # of: ..." comment - the by-hand filing surface for a genealogist
        # with no tools - kept teaching the old, shorter list. Nothing
        # caught the drift because the two were never compared. Every
        # controlled `source_type` value must appear in that comment so a
        # future addition to SOURCE_TYPES fails this test instead of
        # silently going undiscoverable by hand.
        from _lib import SOURCE_TYPES
        tmpl_text = (TEMPLATES / 'sources' / '_TEMPLATE.source.md').read_text(encoding='utf-8')
        comment = '\n'.join(
            ln for ln in tmpl_text.splitlines() if ln.lstrip().startswith('#') or 'source_type:' in ln)
        missing = {t for t in SOURCE_TYPES if t not in comment}
        self.assertEqual(missing, set(),
                          f'_TEMPLATE.source.md comment missing source_type(s): {missing}')

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

    def test_ensure_person_body_sections_never_merges_the_purpose_block_into_existing_prose(self):
        # A pre-#76 stub that already has SOME body (a Biography written
        # before promotion, say) but no title/purpose block/Sources yet is
        # exactly the fixture `PromoteTests` uses. The title and purpose
        # block carry no `##` heading of their own, so appending them at
        # EOF - wherever that currently is - would land them with nothing
        # bounding them off, silently merging into the CONTENT of whatever
        # section happens to be last on disk: `section_bounds`/`fha site`
        # would then read the purpose block as more Biography prose, and
        # `fha packet`'s purpose-block stripper (anchored to right after the
        # H1) would never find it there to strip it - exactly the
        # scaffolding-leaks-into-publication failure #75/SPEC §21b exists to
        # prevent. They must land at the TOP of the body instead, in
        # canonical order, before the section that survived in place.
        pre76_stub = '---\nid: P-de957bcda1\nname: Jane Doe\n---\n\n## Biography\n\nx\n'
        backfilled, added = ensure_person_body_sections(pre76_stub, 'Jane Doe')
        self.assertEqual(added, ['title', 'purpose block', 'Sources', 'Stories',
                                  'Research Notes', 'Friends & Family'])
        idx_title = backfilled.index('# Jane Doe')
        idx_purpose = backfilled.index("This person's record")
        idx_sources = backfilled.index('## Sources')
        idx_biography = backfilled.index('## Biography')
        idx_x = backfilled.index('\nx\n')
        self.assertLess(idx_title, idx_purpose)
        self.assertLess(idx_purpose, idx_sources)
        self.assertLess(idx_sources, idx_biography)
        # And the human's existing prose is truly untouched, not swallowed.
        self.assertGreater(idx_x, idx_biography)
        self.assertIn('\n## Biography\n\nx\n', backfilled)

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


class PersonSectionIsUnfilledTests(unittest.TestCase):
    """#125: the shared placeholder-detection check `fha site` (and any
    future exporter) uses to tell a scaffolded-but-never-written §16 section
    apart from one a human actually filled in."""

    def test_exact_placeholder_is_unfilled(self):
        self.assertTrue(person_section_is_unfilled(
            'Biography',
            "Write their story in plain sentences. Uncited prose is welcome - it's story and\n"
            "context, never treated as proven fact. Mark anything you mean to back up later\n"
            "with `(TODO: import source)` and a tool will keep it on a gentle to-do list."))

    def test_placeholder_tolerates_surrounding_whitespace(self):
        # content is read via _extract_section, which already strips - but
        # this check must not itself demand a caller pre-strip perfectly.
        self.assertTrue(person_section_is_unfilled(
            'Stories', '\n\n*(none yet)*\n\n'))

    def test_real_content_sharing_words_is_not_unfilled(self):
        # The exact-match design point (issue #125's suggested fix): a human
        # rewrite that keeps a few of the scaffold's own words must still
        # count as written, never silently dropped from the page.
        self.assertFalse(person_section_is_unfilled(
            'Biography',
            "Write their story? He already lived one worth telling: born in "
            "1840 in New York."))

    def test_empty_content_is_not_unfilled(self):
        # An actually-empty section is a DIFFERENT case (`_extract_section`
        # already returns None for it before this check ever runs) - this
        # function only answers "is this the scaffold's own text", so an
        # empty string is correctly False here, not True.
        self.assertFalse(person_section_is_unfilled('Biography', ''))

    def test_unknown_heading_is_never_unfilled(self):
        self.assertFalse(person_section_is_unfilled('Not A Real Heading', 'anything'))

    def test_every_scaffolded_heading_recognises_its_own_placeholder(self):
        # Cross-checks person_section_is_unfilled against the SAME
        # PERSON_BODY_SECTIONS pairs ensure_person_body_sections/
        # render_person_body_scaffold write, so the two can never drift:
        # whatever the scaffold writes, this check must recognise as unfilled.
        headings = set(re.findall(r'^## (.+)$', PERSON_BODY_SECTIONS_TEXT, re.M))
        self.assertEqual(
            headings, {'Biography', 'Stories', 'Research Notes', 'Friends & Family'})
        for heading, placeholder in re.findall(
                r'^## ([^\n]+)\n(.*?)(?=\n## |\Z)', PERSON_BODY_SECTIONS_TEXT, re.M | re.S):
            with self.subTest(heading=heading):
                self.assertTrue(person_section_is_unfilled(heading, placeholder.strip()))

    def test_crlf_placeholder_is_still_unfilled(self):
        # A person record is a plain file, and the archive owner's editor may
        # well be Notepad (or git with autocrlf on) - both write CRLF where
        # the scaffold constant has LF. `fha packet` reads the profile with
        # the newline-PRESERVING read_text_exact, so the check meets those
        # `\r`s intact. A byte-for-byte comparison would call this untouched
        # placeholder "real content" and publish the instructions again.
        for heading, placeholder in PERSON_BODY_SECTIONS:
            with self.subTest(heading=heading):
                self.assertTrue(person_section_is_unfilled(
                    heading, placeholder.replace('\n', '\r\n')))

    def test_trailing_whitespace_placeholder_is_still_unfilled(self):
        # Plenty of editors add (or leave) a trailing space on a line when
        # the file is saved. It changes no word of what the section says, so
        # it must not decide whether the section publishes.
        placeholder = dict(PERSON_BODY_SECTIONS)['Biography']
        spaced = '\n'.join(line + '  ' for line in placeholder.split('\n'))
        self.assertTrue(person_section_is_unfilled('Biography', spaced))

    def test_reworded_placeholder_is_treated_as_written(self):
        # The other half of the same rule: tolerance stops at whitespace.
        # A human who actually changed a WORD has written something, and
        # dropping his text would be the worse failure of the two.
        placeholder = dict(PERSON_BODY_SECTIONS)['Biography']
        self.assertFalse(person_section_is_unfilled(
            'Biography', placeholder.replace('their story', 'his story')))


class StripUnfilledPersonSectionsTests(unittest.TestCase):
    """#125 on the two paths that ship the profile body WHOLE rather than
    reading one named section out of it - `fha wikitree` (line by line) and
    `fha packet` (as a file copy). Both need the section cut out at the
    source, which `person_section_is_unfilled` alone cannot do."""

    def test_freshly_scaffolded_body_keeps_only_what_was_written(self):
        # Built from the SAME renderer the scaffold calls, so this can never
        # drift from what a brand-new record actually holds.
        out = strip_unfilled_person_sections(render_person_body_scaffold('Thomas Hartley'))
        self.assertNotIn('Write their story in plain sentences', out)
        self.assertNotIn('Open questions, hunches, and brick walls', out)
        self.assertNotIn("aren't blood relatives", out)
        # Headings are matched as whole LINES, not as substrings: the purpose
        # block's own prose names `## Research Notes` and `## Sources` while
        # telling the owner where things live, and that mention is not a
        # heading.
        headings = re.findall(r'^## (.+)$', out, re.M)
        # Machine-owned and human-authored structure is NOT this function's
        # business: `## Sources` stays exactly where it was.
        self.assertEqual(headings, ['Sources'])
        self.assertIn('# Thomas Hartley', out)

    def test_written_sections_survive_beside_unfilled_ones(self):
        body = ('# Thomas Hartley\n\n'
                '## Biography\n'
                'Born in 1840 in New York, Thomas crossed the plains twice.\n\n'
                '## Stories\n*(none yet)*\n\n'
                '## Research Notes\n'
                'Keep looking in the Carrow County probate index.\n')
        out = strip_unfilled_person_sections(body)
        self.assertIn('## Biography', out)
        self.assertIn('crossed the plains twice', out)
        self.assertIn('## Research Notes', out)
        self.assertIn('Carrow County probate index', out)
        self.assertNotIn('## Stories', out)
        self.assertNotIn('none yet', out)

    def test_empty_section_is_dropped_with_its_heading(self):
        # A bare heading over nothing promises content the record does not
        # have - the same thing the placeholder does, minus the words.
        body = '# X\n\n## Biography\n   \n\n## Stories\nA tale worth keeping.\n'
        self.assertEqual(
            strip_unfilled_person_sections(body),
            '# X\n\n## Stories\nA tale worth keeping.\n')

    def test_unknown_headings_are_never_touched(self):
        # This function decides "was this section written", never "is this
        # section wanted" - a heading the human invented stays put, empty or
        # not, and so does a `## Claims` block.
        body = '# X\n\n## Gravestone Photos\n\n## Claims\n```yaml\n```\n'
        self.assertEqual(strip_unfilled_person_sections(body), body)

    def test_nothing_to_strip_is_a_byte_for_byte_no_op(self):
        body = '# X\n\n## Biography\n\nAn old-shape record.\n'
        self.assertEqual(strip_unfilled_person_sections(body), body)

    def test_crlf_endings_are_preserved(self):
        # `fha packet` copies the profile with read_text_exact /
        # write_text_exact_atomic precisely so line endings survive the trip.
        # Flipping CRLF to LF here would show up as a whole-file diff to
        # anyone comparing the packet copy against the original - and the
        # dropped last section must not leave a lone `\r` behind either.
        body = ('# X\r\n\r\n## Biography\r\nReal prose.\r\n\r\n'
                '## Stories\r\n*(none yet)*\r\n')
        out = strip_unfilled_person_sections(body)
        self.assertEqual(out, '# X\r\n\r\n## Biography\r\nReal prose.\r\n')

    def test_empty_body_does_not_crash(self):
        self.assertEqual(strip_unfilled_person_sections(''), '')


class TemplateHygieneTests(unittest.TestCase):
    def test_no_generated_header(self):
        for rel in ('sources/_TEMPLATE.source.md', 'people/_TEMPLATE.person.md',
                    'people/stubs/_TEMPLATE.stub.md', 'notes/questions.md'):
            text = (TEMPLATES / rel).read_text(encoding='utf-8')
            self.assertNotIn('GENERATED', text, rel)


if __name__ == '__main__':
    unittest.main()
