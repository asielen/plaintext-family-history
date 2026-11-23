"""
test_site_print.py: pages print cleanly (TOOLING 12 "Pages", 7 D11 rule 2).

What a relative sees: printing a person page gives the page, not the menu bar
or the zoom buttons, with the family chart and the fan both on the paper. The
stylesheets are checked by text, the site by building a copy of the
example archive, and the browser helper by `node --check` where node exists.
"""

import contextlib
import importlib.util
import io
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

_spec = importlib.util.spec_from_file_location('fha_site', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
sys.modules['fha_site'] = site
_spec.loader.exec_module(site)

STYLES = ROOT / 'design' / 'styles.css'
VIEW_CSS = ROOT / 'design' / 'view.css'
VENDOR = ROOT / 'tools' / 'templates' / 'vendor'


def _print_block(css: str) -> str:
    """The text inside the last `@media print { ... }` block, braces matched."""
    start = css.rfind('@media print')
    if start < 0:
        return ''
    open_at = css.index('{', start)
    depth = 0
    for i in range(open_at, len(css)):
        if css[i] == '{':
            depth += 1
        elif css[i] == '}':
            depth -= 1
            if depth == 0:
                return css[open_at + 1:i]
    return ''


def _hidden_selectors(block: str) -> list[str]:
    """Every selector in a print-block rule that sets `display: none`."""
    out = []
    for sel, body in re.findall(r'([^{}]+)\{([^}]*)\}', block):
        if re.search(r'display:\s*none', body):
            out += [s.strip() for s in sel.split(',') if s.strip()]
    return out


class PrintStylesheetTests(unittest.TestCase):
    def test_stylesheet_has_a_print_section(self):
        block = _print_block(STYLES.read_text(encoding='utf-8'))
        self.assertTrue(block, 'design/styles.css has no @media print block')
        hide = re.search(r'([^{}]*)\{[^}]*display:\s*none', block)
        self.assertIn('.site-nav', hide.group(1))
        self.assertRegex(block, r'\.print-only\s*\{[^}]*display:\s*block')

    def test_print_hides_the_chart_buttons_and_the_explorer(self):
        block = _print_block(STYLES.read_text(encoding='utf-8'))
        self.assertIn('.fha-tree-controls', block)
        self.assertIn('.fha-tree-explorer', block)

    def test_view_css_has_print_rules_and_stays_self_contained(self):
        css = VIEW_CSS.read_text(encoding='utf-8')
        self.assertTrue(_print_block(css), 'design/view.css has no @media print block')
        rules = re.sub(r'/\*.*?\*/', '', css, flags=re.S)   # the header comment names both
        self.assertNotIn('url(', rules)
        self.assertNotIn('@import', rules)

    def test_a_private_banner_is_not_hidden_on_paper(self):
        block = _print_block(VIEW_CSS.read_text(encoding='utf-8'))
        self.assertNotIn('view-banner', block)


class PrintHelperScriptTests(unittest.TestCase):
    def test_tree_records_its_full_view_box(self):
        js = (VENDOR / 'fha-tree.js').read_text(encoding='utf-8')
        wrap = js[js.index('function wrapStatic'):]
        self.assertIn('dataset.fhaViewbox', wrap)

    def test_helper_opens_charts_and_restores_the_view(self):
        js = (VENDOR / 'fha-site.js').read_text(encoding='utf-8')
        for needle in ('beforeprint', 'afterprint', 'data-fha-viewbox', 'fha-tree-explorer'):
            self.assertIn(needle, js)

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_scripts_parse(self):
        for name in ('fha-site.js', 'fha-tree.js'):
            r = subprocess.run(['node', '--check', str(VENDOR / name)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, name + ': ' + r.stderr)


class PrintSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import index as index_mod
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        root = tmp / 'archive'
        shutil.copytree(ROOT / 'example-archive', root,
                        ignore=shutil.ignore_patterns('generated', '.cache'))
        quiet = dict(stdout=io.StringIO(), stderr=io.StringIO())
        with contextlib.redirect_stdout(quiet['stdout']), contextlib.redirect_stderr(quiet['stderr']):
            index_mod.build_index(root, {})
            cls.out1 = tmp / 'site1'
            cls.res = site.run_site(root, cls.out1, linked=False)
            cls.out2 = tmp / 'site2'
            site.run_site(root, cls.out2, linked=False)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _pages(self):
        pages = sorted(self.out1.rglob('*.html'))
        self.assertGreater(len(pages), 5)
        return pages

    def test_every_page_loads_the_print_helper(self):
        self.assertEqual(self.res['status'], 'ok', self.res['messages'])
        self.assertTrue((self.out1 / 'vendor' / 'fha-site.js').is_file())
        for page in self._pages():
            rel = page.relative_to(self.out1)
            prefix = '/'.join(['..'] * (len(rel.parts) - 1)) or '.'
            html = page.read_text(encoding='utf-8')
            self.assertIn(f'<script src="{prefix}/vendor/fha-site.js" defer></script>', html, rel)

    def test_print_only_footer_line_once_per_page(self):
        for page in self._pages():
            html = page.read_text(encoding='utf-8')
            self.assertEqual(len(re.findall(r'<p class="print-only">Printed from [^<]+\.</p>', html)),
                             1, page.name)

    def test_fan_and_explorer_markup_for_print(self):
        html = '\n'.join(p.read_text(encoding='utf-8')
                         for p in (self.out1 / 'persons').glob('*.html'))
        self.assertIn('class="chart fha-tree-explorer"', html)

    def test_calendar_keeps_its_all_months_heading_on_paper(self):
        # The whole year prints under "All months"; without that heading the
        # list reads as more of "This month in the family", current month twice.
        block = _print_block(STYLES.read_text(encoding='utf-8'))
        hidden = [re.sub(r'\s+', ' ', s) for s in _hidden_selectors(block)]
        for sel in ('.cal-all', '.cal-all > summary', 'details.cal-all'):
            self.assertNotIn(sel, hidden)

    def test_two_builds_are_byte_identical(self):
        a = {p.relative_to(self.out1): p.read_bytes() for p in self.out1.rglob('*') if p.is_file()}
        b = {p.relative_to(self.out2): p.read_bytes() for p in self.out2.rglob('*') if p.is_file()}
        self.assertEqual(sorted(a), sorted(b))
        self.assertEqual([k for k in a if a[k] != b[k]], [])


class PrintWorkbenchTests(unittest.TestCase):
    """He prints Grandpa's page from `fha serve` to hand to a cousin: the
    paper carries no "edit", "add" or "change..." links, no "Add a
    milestone..." buttons and no "add" beside an unknown ancestor in the family chart."""

    @classmethod
    def setUpClass(cls):
        import index as index_mod
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        root = tmp / 'archive'
        shutil.copytree(ROOT / 'example-archive', root,
                        ignore=shutil.ignore_patterns('generated', '.cache'))
        quiet = dict(stdout=io.StringIO(), stderr=io.StringIO())
        with contextlib.redirect_stdout(quiet['stdout']), contextlib.redirect_stderr(quiet['stderr']):
            index_mod.build_index(root, {})
            cls.out = tmp / 'wb'
            cls.res = site.run_site(root, cls.out, linked=True, workbench=True,
                                    workbench_context={'port': 8765, 'csrf_token': '0' * 32,
                                                       'review_count': 0, 'inbox_count': 0})
        cls.hidden = _hidden_selectors(_print_block(STYLES.read_text(encoding='utf-8')))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_edit_controls_are_hidden_on_paper(self):
        self.assertEqual(self.res['status'], 'ok', self.res['messages'])
        for sel in ('[data-wb-open]:not(.ped-empty)', '[data-wb-open-file]', '.wb-record', '.wb-pointer',
                    '.wb-fill', '.wb-fill-note', '[data-wb-open] + .wb-later', '.wb-add-hint'):
            self.assertIn(sel, self.hidden)

    def test_every_edit_control_is_one_the_print_rules_reach(self):
        # A control the rules above do not reach would still print. The only
        # data-wb-open element left on paper is the empty ancestor card, and
        # its "add" words sit in .wb-add-hint so only "Unknown" prints.
        pages = sorted(self.out.rglob('*.html'))
        self.assertGreater(len(pages), 5)
        for page in pages:
            html = page.read_text(encoding='utf-8')
            for tag in re.findall(r'<[a-z]+\b[^>]*\bdata-wb-open=[^>]*>', html):
                if 'ped-empty' in tag:
                    continue
                self.assertRegex(tag, r'^<(a|button)\b', (page.name, tag))
            self.assertNotIn('Unknown &mdash; add', html, page.name)
            for card in re.findall(r'<div [^>]*class="ped-node ped-empty"[^>]*data-wb-open[^>]*>.*?</div>',
                                   html, re.S):
                self.assertIn('class="wb-add-hint"', card, page.name)


if __name__ == '__main__':
    unittest.main()
