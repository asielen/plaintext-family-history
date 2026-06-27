"""
test_consumer_sweep.py — every token reader conforms to the `[[ ]]` grammar
(wikilink-native step 02).

The consumers resolve the new double-bracket form (and `|display` / `#fragment`)
exactly as they did single brackets, and the two privacy-critical renderers
(site, wikitree) handle the new display-text hole: a living person referenced by
`[[P-id]]`, a resolved `[[Name]]`, or a frontmatter `people:` link is redacted
name and all. Tests build a real archive + index so the alias layer is exercised
end to end.
"""

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index
import views
import wikitree

# `import site` would resolve to the stdlib module (it is loaded into
# sys.modules at interpreter startup), so load our tool by file path.
_spec = importlib.util.spec_from_file_location('fha_site', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
sys.modules['fha_site'] = site
_spec.loader.exec_module(site)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


# ── views: clickable sources + places ─────────────────────────────────────────

class ViewsFormattingTests(unittest.TestCase):
    def test_format_sid_is_double_bracket(self):
        self.assertEqual(views._format_sid('s-1111111111'), '[[S-1111111111]]')

    def test_place_with_lid_is_clickable(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE places(id TEXT, name TEXT)')
        conn.execute("INSERT INTO places VALUES ('l-1111111111', 'Fairview')")
        # place_text present + an L-id → linked, text preserved as display.
        self.assertEqual(
            views._place_label('Fairview, Ohio', 'l-1111111111', conn),
            '[[L-1111111111|Fairview, Ohio]]',
        )
        # free place_text, no L-id → plain.
        self.assertEqual(views._place_label('Somewhere', None, conn), 'Somewhere')


# ── wikitree: in-token display vs name folding ────────────────────────────────

class WikitreeDisplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root / 'people' / 'subject.md',
               '---\nid: P-0000000001\nname: John Smith\ntier: curated\nliving: false\n---\n\n'
               '# John Smith\n\n## Biography\n{body}\n\n## Stories\n*(none yet)*\n')
        _write(self.root / 'people' / 'mary.md',
               '---\nid: P-0000000002\nname: Mary Jones\ntier: stub\nliving: false\n---\n')
        _write(self.root / 'sources' / 'm_S-0000000002.md',
               '---\nid: S-0000000002\ntitle: Marriage\nsource_type: vital-record\n'
               'citation: "Marriage record."\n---\n')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _render_body(self, body: str) -> str:
        prof = self.root / 'people' / 'subject.md'
        prof.write_text(prof.read_text(encoding='utf-8').replace('{body}', body), encoding='utf-8')
        # wikitree resolves the WikiTree id for Mary from the index.
        index.build_index(self.root, {})
        conn = sqlite3.connect(str(self.root / '.cache' / 'index.sqlite'))
        conn.execute("INSERT INTO person_external(person_id, system, ext_id) "
                     "VALUES ('p-0000000002','wikitree','Jones-99')")
        conn.commit()
        conn.close()
        r = wikitree.run_wikitree(self.root, 'p-0000000001')
        self.assertEqual(r['status'], 'ok')
        return r['text']

    def test_in_token_display_used_without_doubling(self):
        text = self._render_body('Married [[P-0000000002|Mary Jones]] in 1900 [[S-0000000002]].')
        self.assertIn('[[Jones-99|Mary Jones]]', text)        # display used as link text
        self.assertNotIn('Mary Jones Mary Jones', text)       # not doubled
        self.assertIn('<ref name="S-0000000002"/>', text)     # source → ref

    def test_preceding_name_still_folds_for_bare_token(self):
        text = self._render_body('Married Mary Jones [[P-0000000002]] in 1900.')
        self.assertIn('[[Jones-99|Mary Jones]]', text)
        self.assertNotIn('Mary Jones Mary Jones', text)


# ── site: display preference + redaction of the name-link hole ────────────────

class SiteRedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out = self.root / '.cache' / 'site'
        # Page subject (publishable) whose biography cites a LIVING person three
        # ways: by ID, by resolved name, and by legacy single bracket.
        _write(self.root / 'people' / 'subject.md',
               '---\nid: P-0000000001\nname: Jane Doe\ntier: curated\nliving: false\n---\n\n'
               '# Jane Doe\n\n## Biography\n'
               'Knew [[P-de957bcda1]], also [[Ken Smith]], also legacy [P-de957bcda1]; '
               'and an inert [[grandmas-album]] link.\n')
        _write(self.root / 'people' / 'ken.md',
               '---\nid: P-de957bcda1\nname: Ken Smith\ntier: stub\nliving: unknown\n---\n')
        index.build_index(self.root, {})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build(self, *, linked: bool) -> str:
        import os, time
        future = time.time() + 5
        os.utime(self.root / '.cache' / 'index.sqlite', (future, future))
        site.run_site(self.root, self.out, linked=linked)
        return (self.out / 'persons' / site._page_filename('p-0000000001')).read_text(encoding='utf-8')

    def test_living_person_redacted_every_form(self):
        html = self._build(linked=False)
        self.assertNotIn('Ken Smith', html)            # name never leaks, any form
        self.assertIn('Living Person', html)           # redaction label shown
        self.assertIn('grandmas-album', html)          # inert stem → plain text, kept

    def test_inert_stem_not_marked_as_broken(self):
        html = self._build(linked=False)
        self.assertNotIn('<mark>[grandmas-album]</mark>', html)   # not a broken-citation flag


class SiteDisplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out = self.root / '.cache' / 'site'
        _write(self.root / 'people' / 'subject.md',
               '---\nid: P-0000000001\nname: Jane Doe\ntier: curated\nliving: false\n---\n\n'
               '# Jane Doe\n\n## Biography\nMet [[P-0000000002|Maggie]] once.\n')
        _write(self.root / 'people' / 'mar.md',
               '---\nid: P-0000000002\nname: Margaret Cole\ntier: curated\nliving: false\n---\n\n# Margaret Cole\n')
        index.build_index(self.root, {})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_in_token_display_preferred_over_record_name(self):
        import os, time
        future = time.time() + 5
        os.utime(self.root / '.cache' / 'index.sqlite', (future, future))
        site.run_site(self.root, self.out, linked=True)
        html = (self.out / 'persons' / site._page_filename('p-0000000001')).read_text(encoding='utf-8')
        self.assertIn('>Maggie</a>', html)            # in-token display wins
        self.assertNotIn('>Margaret Cole</a>', html)  # not the record name


if __name__ == '__main__':
    unittest.main()
