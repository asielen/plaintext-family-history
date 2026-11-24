"""
test_views_register.py - `fha views register`, the printed register report.

A relative who reads genealogy books is handed everyone descended from Thomas
Hartley the way the New England Historical and Genealogical Register prints a family:
each person who has children gets a number and a short paragraph, their
children are listed with Roman numerals, and a child whose own family follows
later carries the number to turn to. Numbers run generation by generation.
Living descendants are counted under their parent and never named, and a
living person's own children are left out.

The family is the Hartley example from the family-sheet tests, with
grandchildren added.
"""

import contextlib
import io
import re
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import views
from _lib import EXIT_CLEAN, EXIT_ERRORS, EXIT_FAILURE, EXIT_WARNINGS
from tests import test_views_reports as R

T, M, C1, C2, CL, CU, CR, CS = R.T, R.M, R.C1, R.C2, R.CL, R.CU, R.CR, R.CS
AD = 'p-adadadadad'     # Adam Lowe, adopted by Thomas
G1 = 'p-gagagagaga'     # Anna Hartley, John junior's daughter, b. 1891 (has a child)
G2 = 'p-gbgbgbgbgb'     # Zed Hartley, John junior's son, no birth date
GL = 'p-gkgkgkgkgk'     # a living grandchild of Thomas, John junior's child
GX = 'p-gxgxgxgxgx'     # Deep Hartley: deceased child of the living Lena
GG = 'p-grgrgrgrgr'     # Rose Hartley: Anna's daughter, 1915
MK = 'p-mkmkmkmkmk'     # Mina Hartley: Mary's daughter, 1895 (has a child)
MG = 'p-mgmgmgmgmg'     # Mabel Hartley: Mina's daughter, 1920 (leaf)


class _RegBase(R._SheetBase):
    def people(self):
        return super().people() + [
            (AD, 'Adam Lowe', 'M', 'false', ''),
            (G1, 'Anna Hartley', 'F', 'false', ''),
            (G2, 'Zed Hartley', 'M', 'false', ''),
            (GL, 'Kit Hartley', 'F', 'true', ''),
            (GX, 'Deep Hartley', 'M', 'false', ''),
            (GG, 'Rose Hartley', 'F', 'false', ''),
            (MK, 'Mina Hartley', 'F', 'false', ''),
            (MG, 'Mabel Hartley', 'F', 'false', ''),
        ]

    def sources(self):
        return super().sources() + [
            ('s-rgrgrgrgr1', 'Hartley family Bible, second page', 'Hartley family Bible, p. 2.',
             R._claim('c-adopt00001', 'relationship', [T, AD], '',
                      roles=f'{{child: {AD}, parent: [{T}]}}', subtype='adoptive'), '', None),
            ('s-rgrgrgrgr2', 'Grandchildren register', 'Hartley grandchildren register.',
             R._rel('g1', G1, [C1, CS]) + R._rel('g2', G2, [C1, CS]) + R._rel('gg', GG, [G1])
             + R._rel('mk', MK, [C2]) + R._rel('mg', MG, [MK])
             + R._claim('c-birthG1000', 'birth', [G1], '1891', roles=f'{{child: {G1}}}',
                        place='Fairview')
             + R._claim('c-birthGG000', 'birth', [GG], '1915', roles=f'{{child: {GG}}}')
             + R._claim('c-birthMK000', 'birth', [MK], '1895', roles=f'{{child: {MK}}}')
             + R._claim('c-birthMG000', 'birth', [MG], '1920', roles=f'{{child: {MG}}}'), '', None),
            ('s-rgrgrgrgr3', 'Living cousins register', 'Hartley cousins register.',
             R._rel('gl', GL, [C1, CS]) + R._rel('gx', GX, [CL])
             + R._claim('c-birthGX000', 'birth', [GX], '1925', roles=f'{{child: {GX}}}'), '', None),
        ]

    def run_reg(self, pid=T, generations=None, fmt='md'):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = views.run_register(self.root, pid, generations, fmt)
        return result, out.getvalue(), err.getvalue()

    def reg_text(self, pid=T, generations=None, fmt='md') -> str:
        result, out, err = self.run_reg(pid, generations, fmt)
        self.assertEqual(result.exit_code, EXIT_CLEAN, out + err)
        return Path(result.changed[0]).read_text(encoding='utf-8')

    def paragraph_numbers(self, text: str) -> dict[str, str]:
        """`{number: paragraph}` for each numbered person's paragraph."""
        return {m.group(1): m.group(2) for m in
                re.finditer(r'(?m)^\*\*(\d+)\.\*\* (.*)$', text)}


class RegisterNumberingTests(_RegBase):
    def test_numbers_run_generation_by_generation(self):
        # Depth first would number John's daughter Anna 3 and Mary 4; the
        # register numbers every second-generation person before any third.
        paras = self.paragraph_numbers(self.reg_text())
        self.assertTrue(paras['1'].startswith('Thomas Edward Hartley was born'), paras)
        self.assertTrue(paras['2'].startswith('John Hartley Junior'), paras)
        self.assertTrue(paras['3'].startswith('Mary Hartley'), paras)
        self.assertTrue(paras['4'].startswith('Anna Hartley'), paras)
        self.assertTrue(paras['5'].startswith('Mina Hartley'), paras)
        self.assertEqual(['1', '2', '3', '4', '5'], list(paras))

    def test_a_child_with_children_carries_their_number_in_the_list(self):
        text = self.reg_text()
        self.assertRegex(text, r'(?m)^- 2 i\. John Hartley Junior')
        self.assertRegex(text, r'(?m)^- 3 ii\. Mary Hartley')
        self.assertRegex(text, r'(?m)^- 4 i\. Anna Hartley')

    def test_roman_numerals_follow_birth_order_and_undated_last(self):
        text = self.reg_text()
        self.assertRegex(text, r'(?m)^- ii\. Zed Hartley')   # no birth date: after Anna
        self.assertRegex(text, r'(?m)^- iii\. Adam Lowe \(adopted\)')   # adopted: after birth children

    def test_a_child_with_no_shown_children_gets_no_arabic_number(self):
        text = self.reg_text()
        self.assertRegex(text, r'(?m)^- i\. Rose Hartley')
        self.assertNotRegex(text, r'(?m)^- \d+ i+\. Zed')
        self.assertNotIn('Rose Hartley was born', text)   # no paragraph of her own

    def test_the_last_generation_asked_for_never_carries_a_number(self):
        text = self.reg_text(generations=3)
        paras = self.paragraph_numbers(text)
        self.assertEqual(['1', '2', '3'], list(paras))    # Anna and Mina are in the list only
        self.assertRegex(text, r'(?m)^- i\. Anna Hartley')
        self.assertNotIn('Rose Hartley', text)
        self.assertIn('Descendants beyond 3 generations are not printed.', text)

    def test_generations_must_be_between_one_and_ten(self):
        for bad in (0, 11, -2):
            result, _out, err = self.run_reg(generations=bad)
            self.assertEqual(result.exit_code, EXIT_ERRORS, bad)
            self.assertIn('between 1 and 10', err)
        self.assertEqual(self.run_reg(generations=10)[0].exit_code, EXIT_CLEAN)

    def test_paragraph_is_fixed_fact_sentences(self):
        text = self.reg_text()
        self.assertRegex(
            text,
            r'\*\*1\.\*\* Thomas Edward Hartley was born 14 June 1840 in Fairview, Breton County, '
            r'Ohio \[\d+\], and died 3 March 1941 in Elm Creek, Kansas \[\d+\]\. '
            r'He married Margaret Cole on 2 May 1866 in Fairview \[\d+\]\.')
        self.assertRegex(text, r'He married Mary Ross in 1890 \[\d+\]\.')
        self.assertIn('Children of Thomas Edward Hartley and Margaret Cole:', text)
        self.assertIn('## Generation 2', text)

    def test_a_person_with_no_facts_gets_the_name_alone(self):
        text = self.reg_text()
        self.assertRegex(text, r'(?m)^\*\*4\.\*\* Anna Hartley was born 1891 in Fairview \[\d+\]\.')
        self.assertRegex(text, r'(?m)^\*\*5\.\*\* Mina Hartley was born 1895 \[\d+\]\.')


class RegisterPrivacyTests(_RegBase):
    def test_living_children_are_counted_under_their_own_parent(self):
        text = self.reg_text()
        self.assertEqual(1, text.count('and 2 living children (not named)'))
        self.assertEqual(1, text.count('and 1 living child (not named)'))

    def test_living_people_are_never_named_dated_or_placed(self):
        text = self.reg_text()
        for secret in ('Lena', 'Otto', 'Kit', 'Hidden', '1901'):
            self.assertNotIn(secret, text)

    def test_a_living_childs_deceased_child_is_left_out_and_not_counted(self):
        text = self.reg_text()
        self.assertNotIn('Deep Hartley', text)
        self.assertNotIn('1925', text)
        self.assertEqual(2, len(re.findall(r'living child(?:ren)? \(not named\)', text)))

    def test_every_fact_cites_a_listed_note(self):
        text = self.reg_text()
        body, notes = text.split('## Notes')
        listed = {int(n) for n in re.findall(r'(?m)^(\d+)\. ', notes)}
        cited = {int(x) for ref in re.findall(r'\[(\d+(?:, \d+)*)\]', body) for x in ref.split(', ')}
        self.assertEqual(cited, listed)
        for line in body.splitlines():
            if re.match(r'^(\*\*\d+\.\*\* |- (\d+ )?[ivx]+\. )', line) and ' was born' in line + 'x':
                self.assertRegex(line, r'\[\d+')

    def test_footer_counts_who_is_named_and_who_is_counted(self):
        text = self.reg_text()
        self.assertRegex(text, r'This report names \d+ people and shows 4 generations\.')
        self.assertIn('3 living relatives are counted but not named', text)


class RegisterCousinTests(_RegBase):
    """Anna married her uncle Abel (a child of Thomas): their son Xavier is
    listed in full under the first family that reaches him, and pointed to
    from the other, so no one is printed twice."""

    def people(self):
        return super().people() + [
            ('p-ebebebebeb', 'Abel Hartley', 'M', 'false', ''),
            ('p-xvxvxvxvxv', 'Xavier Hartley', 'M', 'false', ''),
            ('p-qnqnqnqnqn', 'Quinn Hartley', 'M', 'false', ''),
        ]

    def sources(self):
        return super().sources() + [
            ('s-rgrgrgrgr4', 'Abel and Anna register', 'Hartley register, Abel page.',
             R._rel('ab', 'p-ebebebebeb', [T, M]) + R._rel('xa', 'p-xvxvxvxvxv', [G1, 'p-ebebebebeb'])
             + R._claim('c-birthXV000', 'birth', ['p-xvxvxvxvxv'], '1920',
                        roles='{child: p-xvxvxvxvxv}')
             # Quinn is a child of Zed and of the living Lena: a living
             # person's child is never named, even under the dead parent.
             + R._rel('qn', 'p-qnqnqnqnqn', [G2, CL])
             + R._claim('c-birthQN000', 'birth', ['p-qnqnqnqnqn'], '1950',
                        roles='{child: p-qnqnqnqnqn}'), '', None),
        ]

    def test_a_person_on_two_lines_is_listed_in_full_once(self):
        text = self.reg_text()
        self.assertEqual(1, len(re.findall(r'\*\*\d+\.\*\* Xavier Hartley|Xavier Hartley \[\d+\], born', text)))
        self.assertIn('also listed in the family of no.', text)

    def test_a_living_persons_child_is_not_named_under_the_other_parent(self):
        text = self.reg_text()
        self.assertNotIn('Quinn', text)
        self.assertNotIn('1950', text)


class RegisterRefusalTests(_RegBase):
    def test_living_subject_is_refused_and_nothing_written(self):
        result, _out, err = self.run_reg(CL)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('living', err)
        self.assertFalse((self.root / 'generated' / 'views').exists())

    def test_restricted_subject_is_refused(self):
        result, _out, err = self.run_reg(CR)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertNotIn('Hidden', err)

    def test_unknown_id_exits_one(self):
        self.assertEqual(self.run_reg('p-zzzzzzzzzz')[0].exit_code, EXIT_WARNINGS)

    def test_no_index_exits_three(self):
        shutil.rmtree(self.root / '.cache', ignore_errors=True)
        self.assertEqual(self.run_reg()[0].exit_code, EXIT_FAILURE)


class RegisterFileTests(_RegBase):
    def test_rewrite_after_no_change_is_byte_identical(self):
        self.assertEqual(self.reg_text(), self.reg_text())

    def test_file_is_marked_generated_and_carries_the_report_banner(self):
        text = self.reg_text()
        self.assertTrue(text.startswith('<!-- GENERATED'))
        self.assertIn('Living relatives are counted but not named', text)
        self.assertIn('# Register report: descendants of Thomas Edward Hartley', text)
        self.assertTrue((self.root / 'generated' / 'views'
                         / 'report_register_p-aaaaaaaaaa.md').exists())

    def test_html_marks_numbers_children_and_notes(self):
        text = self.reg_text(fmt='html')
        self.assertIn('<p class="ahn-person"><strong>2.</strong>', text)
        self.assertRegex(text, r'<p class="report-child"><strong>2</strong> i\. John Hartley Junior')
        self.assertRegex(text, r'<p class="report-child">ii\. Zed Hartley')
        self.assertIn('<sup class="note">', text)
        self.assertIn('<ol class="report-notes">', text)
        self.assertNotIn('Lena', text)

    def test_views_clean_removes_it(self):
        self.reg_text()
        folder = self.root / 'generated' / 'views'
        self.assertEqual(len(list(folder.iterdir())), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            views.run_clean(self.root)
        self.assertEqual(list(folder.iterdir()), [])


DL = 'p-dkdkdkdkdk'     # Dora Hartley: Mary's deceased daughter whose only children are living
LV1 = 'p-vwvwvwvwv1'
LV2 = 'p-vwvwvwvwv2'
ST = 'p-ststststst'     # Sam Lowe: Edith's son, Thomas's stepson


class RegisterParentOfLivingOnlyTests(_RegBase):
    """A relative must not read that Dora had no children because hers are living."""

    def people(self):
        return super().people() + [
            (DL, 'Dora Hartley', 'F', 'false', ''),
            (LV1, 'Liv One', 'F', 'true', ''),
            (LV2, 'Liv Two', 'M', 'true', ''),
        ]

    def sources(self):
        return super().sources() + [
            ('s-rgrgrgrgr4', 'Dora register', 'Dora page.',
             R._rel('dl', DL, [C2]) + R._rel('l1', LV1, [DL]) + R._rel('l2', LV2, [DL])
             + R._claim('c-birthDL000', 'birth', [DL], '1897', roles=f'{{child: {DL}}}'),
             '', None),
        ]

    def test_a_parent_of_only_living_children_gets_a_number_and_the_count(self):
        text = self.reg_text()
        self.assertRegex(text, r'(?m)^- \d+ ii\. Dora Hartley')
        dora = text.split('Dora Hartley was born', 1)[1].split('## ', 1)[0]
        self.assertIn('- and 2 living children (not named)', dora)
        self.assertNotIn('Liv One', text)
        self.assertNotIn('Liv Two', text)


class RegisterStepChildTests(_RegBase):
    """A stepson is listed with the wife he came with, not the first wife."""

    def people(self):
        return super().people() + [(ST, 'Sam Lowe', 'M', 'false', '')]

    def sources(self):
        return super().sources() + [
            ('s-rgrgrgrgr5', 'Edith marriage and stepson', 'Lowe page.',
             R._claim('c-marrEd0000', 'marriage', [T, R.NEWSPOUSE], '1930',
                      roles=f'{{spouse: [{T}, {R.NEWSPOUSE}]}}')
             + R._claim('c-stepST0000', 'relationship', [ST, T], '',
                        roles=f'{{child: {ST}, parent: [{T}]}}', subtype='step')
             + R._rel('st', ST, [R.NEWSPOUSE])
             + R._claim('c-birthST000', 'birth', [ST], '1915', roles=f'{{child: {ST}}}'),
             '', None),
        ]

    def test_a_stepchild_is_listed_under_the_spouse_he_came_with(self):
        text = self.reg_text()
        first = text.split('Children of Thomas Edward Hartley and Margaret Cole:')[1].split('\n\n')[0]
        self.assertNotIn('Sam Lowe', first)
        second = text.split('Children of Thomas Edward Hartley and Edith Lowe:')[1].split('\n\n')[0]
        self.assertRegex(second, r'(?m)^- i\. Sam Lowe \(step\)')


class RegisterOneGenerationTests(_RegBase):
    def test_one_generation_prints_only_the_person(self):
        text = self.reg_text(generations=1)
        self.assertNotIn('Children of', text)
        self.assertNotIn('John Hartley Junior', text)
        self.assertIn('shows 1 generation.', text)
        self.assertIn('Descendants beyond 1 generations are not printed.', text)


class RegisterCommandTests(unittest.TestCase):
    def test_register_is_a_views_subcommand(self):
        import argparse
        parser = argparse.ArgumentParser()
        views.register(parser.add_subparsers())
        args = parser.parse_args(['views', 'register', 'p-aaaaaaaaaa', '--generations', '3'])
        self.assertEqual(3, args.generations)
        self.assertEqual('md', args.format)


if __name__ == '__main__':
    unittest.main()
