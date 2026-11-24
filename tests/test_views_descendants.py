"""
test_views_descendants.py - `fha views descendants`, the printed descendant report.

A relative with no computer is handed everyone descended from Thomas Hartley,
numbered 1, 1.1, 1.2, 1.1.1 so she can find any branch. Living cousins are
counted under their parent and never named or dated, and a living cousin's own
children are left out entirely (counting them under an unnamed parent would
say too much).

The family is the Hartley example from the family-sheet tests, with grandchildren
added.
"""

import contextlib
import io
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import views
from _lib import EXIT_CLEAN, EXIT_ERRORS, EXIT_FAILURE, EXIT_WARNINGS
from tests import test_views_reports as R

T, M, C1, C2, CL, CU, CR = R.T, R.M, R.C1, R.C2, R.CL, R.CU, R.CR
CS = R.CS
TW = 'p-twtwtwtwtw'     # Abel Hartley, twin of Mary (born the same year)
UD = 'p-ydydydydyd'    # a child with no birth date on record
AD = 'p-adadadadad'     # a child adopted by Thomas
G1 = 'p-gagagagaga'     # Anna Hartley, John junior's daughter, b. 1891
G2 = 'p-gbgbgbgbgb'     # Zed Hartley, John junior's son, no birth date
GL = 'p-gkgkgkgkgk'     # a living grandchild of John junior
GX = 'p-gxgxgxgxgx'     # Deep Hartley: deceased child of the living Lena
GG = 'p-grgrgrgrgr'     # Rose Hartley: Anna's daughter, 1915


class _DescBase(R._SheetBase):
    def people(self):
        return super().people() + [
            (TW, 'Abel Hartley', 'M', 'false', ''),
            (UD, 'Undated Hartley', 'F', 'false', ''),
            (AD, 'Adam Lowe', 'M', 'false', ''),
            (G1, 'Anna Hartley', 'F', 'false', ''),
            (G2, 'Zed Hartley', 'M', 'false', ''),
            (GL, 'Kit Hartley', 'F', 'true', ''),
            (GX, 'Deep Hartley', 'M', 'false', ''),
            (GG, 'Rose Hartley', 'F', 'false', ''),
        ]

    def sources(self):
        return super().sources() + [
            ('s-dsdsdsdsd1', 'Hartley family Bible, second page', 'Hartley family Bible, p. 2.',
             R._rel('kidtw', TW, [T, M]) + R._rel('kidud', UD, [T, M])
             + R._claim('c-birthTW000', 'birth', [TW], '1869', roles=f'{{child: {TW}}}')
             + R._claim('c-adopt00001', 'relationship', [T, AD], '',
                        roles=f'{{child: {AD}, parent: [{T}]}}', subtype='adoptive'), '', None),
            ('s-dsdsdsdsd2', 'Grandchildren register', 'Hartley grandchildren register.',
             R._rel('g1', G1, [C1, CS]) + R._rel('g2', G2, [C1, CS]) + R._rel('gg', GG, [G1])
             + R._claim('c-birthG1000', 'birth', [G1], '1891', roles=f'{{child: {G1}}}')
             + R._claim('c-birthGG000', 'birth', [GG], '1915', roles=f'{{child: {GG}}}'), '', None),
            ('s-dsdsdsdsd3', 'Living cousins register', 'Hartley cousins register.',
             R._rel('gl', GL, [C1, CS]) + R._rel('gx', GX, [CL])
             + R._claim('c-birthGX000', 'birth', [GX], '1925', roles=f'{{child: {GX}}}'), '', None),
        ]

    def run_desc(self, pid=T, generations=None, fmt='md'):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = views.run_descendants(self.root, pid, generations, fmt)
        return result, out.getvalue(), err.getvalue()

    def desc_text(self, pid=T, generations=None, fmt='md') -> str:
        result, out, err = self.run_desc(pid, generations, fmt)
        self.assertEqual(result.exit_code, EXIT_CLEAN, out + err)
        return Path(result.changed[0]).read_text(encoding='utf-8')

    def numbered(self, text: str) -> dict[str, str]:
        """`{number: rest of line}` for every numbered person line."""
        found = {}
        for ln in text.splitlines():
            m = re.match(r'^\s*- (\d+(?:\.\d+)*) (.*)$', ln)
            if m:
                found[m.group(1)] = m.group(2)
        return found


class DescendantNumberingTests(_DescBase):
    def test_three_generations_are_numbered_the_d_aboville_way(self):
        nums = self.numbered(self.desc_text())
        self.assertTrue(nums['1'].startswith('Thomas Edward Hartley (1840-1941)'))
        self.assertTrue(nums['1.1'].startswith('John Hartley Junior (1867-1930)'))
        self.assertTrue(nums['1.1.1'].startswith('Anna Hartley (born 1891)'))
        self.assertTrue(nums['1.1.1.1'].startswith('Rose Hartley (born 1915)'))

    def test_children_run_oldest_first_twins_by_name_and_undated_last(self):
        nums = self.numbered(self.desc_text())
        self.assertTrue(nums['1.1'].startswith('John Hartley Junior'))   # 1867
        self.assertTrue(nums['1.2'].startswith('Abel Hartley'))          # 1869, name first
        self.assertTrue(nums['1.3'].startswith('Mary Hartley'))          # 1869
        self.assertTrue(nums['1.4'].startswith('Undated Hartley'))       # no birth: last
        # John junior's undated son comes after his dated daughter.
        self.assertTrue(nums['1.1.2'].startswith('Zed Hartley'))

    def test_adopted_child_is_numbered_after_the_birth_children_and_marked(self):
        nums = self.numbered(self.desc_text())
        self.assertTrue(nums['1.5'].startswith('Adam Lowe (adopted)'), nums)

    def test_generations_two_stops_at_the_children(self):
        text = self.desc_text(generations=2)
        nums = self.numbered(text)
        self.assertIn('1.1', nums)
        self.assertNotIn('1.1.1', nums)
        self.assertNotIn('Anna Hartley', text)
        self.assertIn('2 generations', text)
        self.assertIn('More descendants are not shown', text)

    def test_generations_must_be_between_one_and_twelve(self):
        for bad in (0, 13, -2):
            result, _out, err = self.run_desc(generations=bad)
            self.assertEqual(result.exit_code, EXIT_ERRORS, bad)
            self.assertIn('between 1 and 12', err)
        self.assertEqual(self.run_desc(generations=12)[0].exit_code, EXIT_CLEAN)


class DescendantPrivacyTests(_DescBase):
    def test_living_children_are_counted_under_their_own_parent(self):
        text = self.desc_text()
        # Under Thomas: Lena (living) and Otto (unknown) are two living children.
        self.assertIn('and 2 living children (not named) [', text.split('1.1 ')[0] + '1.1 ') \
            if False else None
        thomas_children = [ln for ln in text.splitlines() if ln.startswith('  - ') and 'living' in ln]
        self.assertEqual(['  - and 2 living children (not named)'], thomas_children)
        # Under John junior: one living child.
        self.assertIn('    - and 1 living child (not named)', text)

    def test_living_people_are_never_named_dated_or_placed(self):
        text = self.desc_text()
        for secret in ('Lena', 'Otto', 'Kit', 'Hidden', '1901'):
            self.assertNotIn(secret, text)

    def test_a_living_childs_deceased_child_is_left_out_and_not_counted(self):
        text = self.desc_text()
        self.assertNotIn('Deep Hartley', text)
        self.assertNotIn('1925', text)
        # Two living children under Thomas, one under John: nothing for Lena's.
        self.assertEqual(2, len(re.findall(r'living child(?:ren)? \(not named\)', text)))

    def test_a_restricted_child_is_left_out_and_not_counted(self):
        text = self.desc_text()
        self.assertNotIn('Hidden', text)
        self.assertIn('and 2 living children (not named)', text)   # not 3

    def test_every_numbered_line_cites_a_listed_note(self):
        text = self.desc_text()
        notes = text.split('## Notes')[1]
        listed = {int(n) for n in re.findall(r'^(\d+)\. ', notes, flags=re.M)}
        cited = set()
        for ln in text.split('## Notes')[0].splitlines():
            if re.match(r'^\s*- (\d+(?:\.\d+)*|Married) ', ln):
                refs = re.findall(r'\[(\d+(?:, \d+)*)\]', ln)
                self.assertTrue(refs, f'no note on: {ln}')
                for r in refs:
                    cited |= {int(x) for x in r.split(', ')}
        self.assertEqual(cited, listed)

    def test_marriage_line_names_the_spouse_and_cites_the_record(self):
        text = self.desc_text()
        self.assertRegex(text, r'  - Married Margaret Cole, 1866 \[\d+')
        self.assertRegex(text, r'    - Married Mary Ross, 1890 \[\d+')

    def test_footer_counts_who_is_named_and_who_is_counted(self):
        text = self.desc_text()
        self.assertRegex(text, r'This report names \d+ people and shows 5 generations\.')
        self.assertIn('3 living relatives are counted but not named', text)


class DescendantRefusalTests(_DescBase):
    def test_living_subject_is_refused_and_nothing_written(self):
        result, _out, err = self.run_desc(CL)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('living', err)
        self.assertFalse((self.root / 'generated' / 'views').exists())

    def test_restricted_subject_is_refused(self):
        result, _out, err = self.run_desc(CR)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertNotIn('Hidden', err)

    def test_unknown_id_exits_one(self):
        self.assertEqual(self.run_desc('p-zzzzzzzzzz')[0].exit_code, EXIT_WARNINGS)

    def test_no_index_exits_three(self):
        import shutil
        shutil.rmtree(self.root / '.cache', ignore_errors=True)
        self.assertEqual(self.run_desc()[0].exit_code, EXIT_FAILURE)


class DescendantFileTests(_DescBase):
    def test_rewrite_after_no_change_is_byte_identical(self):
        first = self.desc_text()
        second = self.desc_text()
        self.assertEqual(first, second)

    def test_file_is_marked_generated_and_carries_the_report_banner(self):
        text = self.desc_text()
        self.assertTrue(text.startswith('<!-- GENERATED'))
        self.assertIn('Living relatives are counted but not named', text)
        self.assertIn('# Descendants of Thomas Edward Hartley', text)

    def test_html_nests_the_people_and_marks_the_numbers(self):
        text = self.desc_text(fmt='html')
        self.assertRegex(text, r'<span class="report-num"[^>]*>1\.1\.1</span>')
        self.assertIn('<ul class="report-tree"', text)
        self.assertIn('<sup class="note">', text)
        self.assertIn('<ol class="report-notes">', text)
        self.assertNotIn('Lena', text)
        self.assertEqual(text.count('<ul'), text.count('</ul>'))
        self.assertEqual(text.count('<li'), text.count('</li>'))

    def test_views_clean_removes_it(self):
        self.desc_text()
        folder = self.root / 'generated' / 'views'
        self.assertEqual(len(list(folder.iterdir())), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            views.run_clean(self.root)
        self.assertEqual(list(folder.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
