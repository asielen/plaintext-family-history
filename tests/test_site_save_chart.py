"""
test_site_save_chart.py: a relative can save a chart as a picture or a drawing.

What a relative sees: under each family chart on the shared site there are two
buttons, one that saves a picture (PNG) to email or print, one that saves a
drawing (SVG) that stays sharp at poster size. The saved file holds only what
the chart on the page shows: a private card stays "Private", and no ids, links
or page data ride along in the file or its name. The browser work is checked
by reading the script; the markup by building a copy of the example archive.
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

VENDOR = ROOT / 'tools' / 'templates' / 'vendor'
SAVE_JS = VENDOR / 'fha-save-chart.js'


class SaveChartScriptTests(unittest.TestCase):
    def setUp(self):
        self.js = SAVE_JS.read_text(encoding='utf-8')

    def test_script_exists_with_both_buttons_and_the_failure_message(self):
        for text in ('Save as a picture (PNG)', 'Save as a drawing (SVG)',
                     'This browser would not make a picture of this chart.',
                     'Pictures of people are left out of the saved chart.'):
            self.assertIn(text, self.js)

    def test_script_credits_topola_viewer(self):
        self.assertIn('topola-viewer', self.js)
        self.assertIn('Apache License 2.0', self.js)

    def test_script_makes_no_network_request(self):
        body = self.js.replace('http://www.w3.org', '')
        for banned in ('fetch(', 'XMLHttpRequest', 'http://', 'https://', 'localStorage'):
            self.assertNotIn(banned, body)

    def test_saved_file_drops_links_pictures_and_page_data(self):
        for needle in ("'data-'", 'unwrap', 'img, image'):
            self.assertIn(needle, self.js)

    def test_saved_file_drops_tooltips_comments_hidden_nodes_and_stray_ids(self):
        # Checked in Chrome on the example site: nothing the page hides or
        # words only in a tooltip rides along in the saved drawing.
        for needle in ("'title, desc, script, style, metadata'", 'nodeType === 8',
                       "cs.visibility === 'hidden'", "cs.display === 'none'",
                       "n === 'id' && !wanted[a.value]", "charAt(0) !== '#'",
                       "n.indexOf('on') === 0"):
            self.assertIn(needle, self.js)

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_script_parses(self):
        r = subprocess.run(['node', '--check', str(SAVE_JS)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_picture_size_stays_inside_browser_canvas_limits(self):
        # The pure size rule runs in node: a poster-sized chart is scaled
        # down so the browser can still make the picture.
        code = ("var c=require(process.argv[1]);"
                "var s=c.pngScale(1000,800);"
                "var b=c.pngScale(30000,30000);"
                "if(Math.abs(s-2.4)>1e-9)throw new Error('small chart scale '+s);"
                "if(!(b>0&&b<1&&b*30000*b*30000<=268435456+1))throw new Error('big '+b);"
                "if(c.pngScale(0,0)!==1)throw new Error('empty');")
        r = subprocess.run(['node', '-e', code, str(SAVE_JS)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class ChartTagTests(unittest.TestCase):
    def test_tag_adds_the_name_to_the_root_svg_only(self):
        out = site._tag_chart('<svg class="x"><svg></svg></svg>', 'a-b')
        self.assertTrue(out.startswith('<svg class="x" data-fha-chart="a-b">'))
        self.assertEqual(out.count('data-fha-chart'), 1)

    def test_tagging_leaves_a_fan_drawn_at_any_angle_as_it_is(self):
        labels = {1: {'name': 'A B', 'surname': 'B'}, 2: {'name': 'C D', 'surname': 'D'}}
        for deg in (180, 270, 360):
            svg = site._render_fan_svg(labels, 2, span_deg=deg)
            tagged = site._tag_chart(svg, 'a-b-ancestor-fan')
            self.assertEqual(tagged.replace(' data-fha-chart="a-b-ancestor-fan"', '', 1), svg)

    def test_empty_name_leaves_the_markup_alone(self):
        self.assertEqual(site._tag_chart('<svg></svg>', ''), '<svg></svg>')

    def test_chart_name_is_a_plain_ascii_slug(self):
        self.assertEqual(site._chart_name('Margaret Cole', 'ancestor-fan'), 'margaret-cole-ancestor-fan')
        self.assertEqual(site._chart_name('', 'family-chart'), 'family-chart')
        self.assertEqual(site._chart_name('Zoë O’Neil', 'family-chart'), 'zoe-oneil-family-chart')
        self.assertEqual(site._chart_name('Józef Kowalski', 'ancestor-fan'), 'jozef-kowalski-ancestor-fan')
        self.assertEqual(site._chart_name('María-José Ruiz', 'ancestor-fan'), 'maria-jose-ruiz-ancestor-fan')


class SaveChartSiteTests(unittest.TestCase):
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
        return sorted(self.out1.rglob('*.html'))

    def test_vendor_script_is_copied_and_every_page_loads_it(self):
        self.assertEqual(self.res['status'], 'ok', self.res['messages'])
        self.assertTrue((self.out1 / 'vendor' / 'fha-save-chart.js').is_file())
        for page in self._pages():
            rel = page.relative_to(self.out1)
            prefix = '/'.join(['..'] * (len(rel.parts) - 1)) or '.'
            self.assertIn(f'<script src="{prefix}/vendor/fha-save-chart.js" defer></script>',
                          page.read_text(encoding='utf-8'), rel)

    def test_every_named_chart_has_one_save_spot_and_the_reverse(self):
        seen = 0
        for page in self._pages():
            html = page.read_text(encoding='utf-8')
            names = re.findall(r'<svg[^>]*\bdata-fha-chart="([^"]+)"', html)
            spots = re.findall(r'<div class="chart-save" data-for="([^"]+)"></div>', html)
            seen += len(names)
            # The family explorer draws its svg in the browser, so only
            # its save spot is in the file.
            for n in names:
                self.assertIn(n, spots, page.name)
            for sp in spots:
                self.assertTrue(sp in names or sp.endswith('-family'), (page.name, sp))
            self.assertEqual(len(spots), len(set(spots)), page.name)
        self.assertGreater(seen, 2)

    def test_person_page_charts_are_named_for_the_person(self):
        html = '\n'.join(p.read_text(encoding='utf-8') for p in (self.out1 / 'persons').glob('*.html'))
        self.assertRegex(html, r'<svg [^>]*data-fha-chart="[a-z0-9-]+-family-chart"')
        self.assertRegex(html, r'<svg [^>]*data-fha-chart="[a-z0-9-]+-ancestor-fan"')
        self.assertRegex(html, r'data-for="[a-z0-9-]+-family"')

    def test_home_tree_is_named_for_the_family(self):
        html = (self.out1 / 'index.html').read_text(encoding='utf-8')
        self.assertRegex(html, r'<svg [^>]*data-fha-chart="[a-z0-9-]*family-tree"')

    def test_names_carry_nothing_but_a_slug(self):
        for page in self._pages():
            for n in re.findall(r'data-fha-chart="([^"]*)"', page.read_text(encoding='utf-8')):
                self.assertRegex(n, r'^[a-z0-9]+(-[a-z0-9]+)*$')
                self.assertNotRegex(n, r'(^|-)[ps]-[a-z0-9]{3,}')

    def test_two_builds_are_byte_identical(self):
        a = {p.relative_to(self.out1): p.read_bytes() for p in self.out1.rglob('*') if p.is_file()}
        b = {p.relative_to(self.out2): p.read_bytes() for p in self.out2.rglob('*') if p.is_file()}
        self.assertEqual([k for k in a if a[k] != b.get(k)], [])


if __name__ == '__main__':
    unittest.main()
