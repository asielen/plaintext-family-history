"""
test_relate_dna.py - `fha relate`'s DNA line and `fha relate --cm`: how much
DNA relatives usually share, and which relationships a match's centimorgans
fit. Always several answers, never a percentage (SPEC §21: nothing here reads
or stores a DNA source). Temp fixtures only.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))

import relate
import _lib
from _lib import load_fha_yaml, EXIT_CLEAN, EXIT_ERRORS
import test_relate as tr

# A double-cousin pair: two brothers married two sisters, so their children
# are related by two separate routes.
X1, X2, Y1, Y2 = 'P-1a1a1a1a1a', 'P-1b1b1b1b1b', 'P-1c1c1c1c1c', 'P-1d1d1d1d1d'
GP1, GP2, GP3, GP4 = 'P-2a2a2a2a2a', 'P-2b2b2b2b2b', 'P-2c2c2c2c2c', 'P-2d2d2d2d2d'
C3, C4 = 'P-3a3a3a3a3a', 'P-3b3b3b3b3b'
_SEX2 = {X1: 'M', X2: 'M', Y1: 'F', Y2: 'F', GP1: 'M', GP2: 'F', GP3: 'M',
         GP4: 'F', C3: 'M', C4: 'F'}
_NAME2 = {X1: 'Xavier One', X2: 'Xander Two', Y1: 'Yara One', Y2: 'Yvette Two',
          GP1: 'Gus Pa', GP2: 'Gwen Pa', GP3: 'Gil Qa', GP4: 'Gail Qa',
          C3: 'Cass Three', C4: 'Cora Four'}


def _build_double() -> Path:
    claims = (
        tr._child_of('C-d000000001', X1, [GP1, GP2])
        + tr._child_of('C-d000000002', X2, [GP1, GP2])
        + tr._child_of('C-d000000003', Y1, [GP3, GP4])
        + tr._child_of('C-d000000004', Y2, [GP3, GP4])
        + tr._child_of('C-d000000005', C3, [X1, Y1])
        + tr._child_of('C-d000000006', C4, [X2, Y2]))
    return tr._write_archive(claims, _SEX2, _NAME2)


def _run(root, a, b, cm=None, use_json=False):
    """Run the command the way the CLI does; return (exit, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        res = relate.run_relate(root, load_fha_yaml(root), a, b, cm=cm)
        code = relate._cmd_relate(res, use_json=use_json)
    return res, code, out.getvalue(), err.getvalue()


def _all_strings(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _all_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _all_strings(v)
    elif isinstance(value, str):
        yield value


class DnaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.family = tr._build_family()
        cls.double = _build_double()

    def test_expected_line_for_first_cousins(self):
        res, code, out, _ = _run(self.family, tr.A, tr.B)
        self.assertEqual(code, EXIT_CLEAN)
        self.assertEqual(res.data['dna']['expected'][0]['key'], 'first_cousin')
        self.assertIn('866', out)
        self.assertIn('1,397', out)

    def test_possibly_half_shows_both_rows(self):
        res, _, out, _ = _run(self.family, tr.A, tr.MAYBE)
        keys = [r['key'] for r in res.data['dna']['expected']]
        self.assertEqual(keys, ['full_sibling', 'half_sibling'])
        self.assertIn('half-siblings', out)

    def test_more_than_one_route_says_may_share_more(self):
        res, _, out, _ = _run(self.double, C3, C4)
        self.assertTrue(res.data['dna']['more_than_one_route'])
        self.assertIn('more than one route', out)

    def test_one_route_does_not_say_it(self):
        res, _, out, _ = _run(self.family, tr.A, tr.B)
        self.assertFalse(res.data['dna']['more_than_one_route'])
        self.assertNotIn('more than one route', out)

    def test_no_blood_route_has_no_dna(self):
        res, _, out, _ = _run(self.family, tr.A, tr.AW)
        self.assertIsNone(res.data['dna'])
        self.assertNotIn('DNA:', out)

    def test_cm_fits_recorded(self):
        res, _, out, _ = _run(self.family, tr.A, tr.B, cm='450')
        self.assertTrue(res.data['dna']['fits_recorded'])
        self.assertIn('450 cM fits first cousins', out)
        self.assertIn('Documents decide', out)

    def test_cm_outside_recorded(self):
        res, _, out, _ = _run(self.family, tr.A, tr.B, cm='50')
        self.assertFalse(res.data['dna']['fits_recorded'])
        self.assertIn('outside what first cousins usually share', out)

    def test_lookup_only_needs_no_index(self):
        bare = Path(tempfile.mkdtemp())
        (bare / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        res = relate.run_relate(bare, load_fha_yaml(bare), None, None, cm='450')
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        fits = res.data['fits']
        self.assertTrue(fits)
        gaps = [abs(f['average_cm'] - 450) for f in fits]
        self.assertEqual(gaps, sorted(gaps))

    def test_lookup_text_has_credit_and_closest_first(self):
        bare = Path(tempfile.mkdtemp())
        (bare / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        out = io.StringIO()
        with redirect_stdout(out):
            res = relate.run_relate(bare, load_fha_yaml(bare), None, None, cm='450')
            relate._cmd_relate(res)
        text = out.getvalue()
        self.assertIn('half first cousins', text)
        self.assertLess(text.index('half first cousins'), text.index('first cousins once removed'))
        self.assertIn('Bettinger', text)
        self.assertIn('documents decide', text.lower())

    def test_lookup_notes_for_tiny_and_huge(self):
        bare = Path(tempfile.mkdtemp())
        (bare / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        small = relate.run_relate(bare, load_fha_yaml(bare), None, None, cm='10')
        self.assertIn('under 20 cM', small.data['note'])
        big = relate.run_relate(bare, load_fha_yaml(bare), None, None, cm='5000')
        self.assertIn('more than a parent and child', big.data['note'])
        self.assertEqual(big.data['fits'], [])

    def test_never_a_percentage(self):
        bare = Path(tempfile.mkdtemp())
        (bare / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        runs = [(bare, None, None, '450'), (bare, None, None, '10'), (bare, None, None, '5000'),
                (self.family, tr.A, tr.B, None), (self.family, tr.A, tr.B, '450'),
                (self.family, tr.A, tr.MAYBE, '1700')]
        for root, a, b, cm in runs:
            res, _, out, _ = _run(root, a, b, cm=cm)
            self.assertNotIn('%', out)
            self.assertNotIn('percent', out.lower())
            for s in _all_strings(res.as_dict()):
                self.assertNotIn('%', s)
                self.assertNotIn('percent', s.lower())

    def test_json_is_serializable(self):
        res, _, out, _ = _run(self.family, tr.A, tr.B, cm='450', use_json=True)
        data = json.loads(out)
        self.assertIn('credit', data['data']['dna'])

    def test_bad_cm_refused(self):
        for bad in ('abc', '-3', ''):
            res, code, _, err = _run(self.family, tr.A, tr.B, cm=bad)
            self.assertEqual(code, EXIT_ERRORS, bad)
            self.assertIn('--cm needs a number of centimorgans, for example --cm 450', err)

    def test_cm_with_one_person_refused(self):
        res, code, _, err = _run(self.family, tr.A, None, cm='450')
        self.assertEqual(code, EXIT_ERRORS)
        self.assertIn('--cm', err)

    def test_one_person_without_cm_refused(self):
        res, code, _, err = _run(self.family, tr.A, None)
        self.assertEqual(code, EXIT_ERRORS)

    def test_cli_lookup_only(self):
        import subprocess
        bare = Path(tempfile.mkdtemp())
        (bare / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        p = subprocess.run([sys.executable, str(ROOT / 'tools' / 'relate.py'), '--cm', '450',
                            '--root', str(bare)], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('450 cM of shared DNA', p.stdout)


class CmHelperTests(unittest.TestCase):
    def test_forgiving_cm(self):
        self.assertEqual(_lib.parse_cm('1,234 cM'), 1234)
        self.assertEqual(_lib.parse_cm(' 450cm '), 450)
        self.assertEqual(_lib.parse_cm('72.5'), 72.5)
        for bad in ('abc', '-3', '', None, 'nan', 'inf'):
            self.assertIsNone(_lib.parse_cm(bad), bad)

    def test_table_rows_are_ordered(self):
        table = _lib.shared_cm_table()
        self.assertTrue(table['credit'])
        rows = table['rows']
        keys = [r['key'] for r in rows]
        self.assertEqual(len(keys), len(set(keys)))
        shapes = [(r['near'], r['far'], r['half']) for r in rows]
        self.assertEqual(len(shapes), len(set(shapes)))
        for r in rows:
            self.assertLessEqual(r['low'], r['average'])
            self.assertLessEqual(r['average'], r['high'])

    def test_expected_lookup_orders_generations(self):
        self.assertEqual(_lib.expected_shared_cm(2, 1, False)['key'], 'aunt_uncle')
        self.assertEqual(_lib.expected_shared_cm(1, 2, False)['key'], 'aunt_uncle')
        self.assertIsNone(_lib.expected_shared_cm(9, 9, False))

    def test_cm_fits_sorted_by_closeness(self):
        fits = _lib.cm_fits(450)
        gaps = [abs(f['average'] - 450) for f in fits]
        self.assertEqual(gaps, sorted(gaps))
        self.assertTrue(all(f['low'] <= 450 <= f['high'] for f in fits))


if __name__ == '__main__':
    unittest.main()
