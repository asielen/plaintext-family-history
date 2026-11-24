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

    def test_double_cousins_above_single_range_is_not_a_misfit(self):
        # 1,700 cM is normal for double first cousins, though above the
        # single first-cousin range; the second route is in the tree.
        res, _, out, _ = _run(self.double, C3, C4, cm='1700')
        self.assertIsNone(res.data['dna']['fits_recorded'])
        self.assertNotIn('does not show', out)
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
        self.assertEqual(_lib.parse_cm('12,345.5'), 12345.5)
        for bad in ('abc', '-3', '', None, 'nan', 'inf', '1,2,3', '12,34'):
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
        self.assertIsNone(_lib.expected_shared_cm(10, 10, False))

    def test_cm_fits_sorted_by_closeness(self):
        fits = _lib.cm_fits(450)
        gaps = [abs(f['average'] - 450) for f in fits]
        self.assertEqual(gaps, sorted(gaps))
        self.assertTrue(all(f['low'] <= 450 <= f['high'] for f in fits))


def _table_without(*keys):
    """The bundled table minus some rows: a tie the chart gives no figure for."""
    t = dict(_lib.shared_cm_table())
    t['rows'] = [r for r in t['rows'] if r['key'] not in keys]
    return t


class MissingRowTests(unittest.TestCase):
    """A tie the chart has no figure for must not read as evidence against
    it, and must not claim the two share little DNA."""

    @classmethod
    def setUpClass(cls):
        cls.family = tr._build_family()

    def _run_without(self, keys, a, b, cm=None):
        from unittest import mock
        with mock.patch.object(relate, 'shared_cm_table', return_value=_table_without(*keys)):
            return _run(self.family, a, b, cm=cm)

    def test_no_row_does_not_say_they_share_little(self):
        res, _, out, _ = self._run_without(['first_cousin'], tr.A, tr.B)
        self.assertEqual(res.data['dna']['expected'], [])
        self.assertNotIn('little', out)
        self.assertNotIn('distant', out)
        self.assertIn('no figure', out)

    def test_no_row_with_cm_is_unknown_not_a_misfit(self):
        res, _, out, _ = self._run_without(['first_cousin'], tr.A, tr.B, cm='450')
        self.assertIsNone(res.data['dna']['fits_recorded'])
        self.assertIn('450 cM', out)
        self.assertIn('half first cousins', out)
        self.assertIn('Documents decide', out)

    def test_maybe_half_with_half_row_missing_is_unknown(self):
        res, _, out, _ = self._run_without(['half_sibling'], tr.A, tr.MAYBE, cm='1000')
        self.assertIsNone(res.data['dna']['fits_recorded'])
        self.assertIn('outside what full siblings usually share', out)
        self.assertIn('no figure for the half form', out)

    def test_maybe_half_with_half_row_missing_still_fits_full(self):
        res, _, out, _ = self._run_without(['half_sibling'], tr.A, tr.MAYBE, cm='2600')
        self.assertTrue(res.data['dna']['fits_recorded'])


class ChartCoverageTests(unittest.TestCase):
    """Rows read from the Shared cM Project 4.0 chart beyond the first 18."""

    def test_more_chart_rows(self):
        want = {(1, 4, False): (420, 186, 713), (1, 3, True): (431, 184, 668),
                (1, 4, True): (208, 103, 284), (2, 3, True): (224, 62, 469),
                (3, 3, True): (120, 10, 325), (2, 5, False): (117, 25, 238),
                (9, 9, False): (11, 0, 42)}
        for (n, f, h), figs in want.items():
            row = _lib.expected_shared_cm(n, f, h)
            self.assertIsNotNone(row, (n, f, h))
            self.assertEqual((row['average'], row['low'], row['high']), figs, (n, f, h))

    def test_chart_has_no_great_great_grandparent_figure(self):
        self.assertIsNone(_lib.expected_shared_cm(0, 4, False))


class FitsListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.family = tr._build_family()

    def test_also_fits_never_silently_cut(self):
        res, _, out, _ = _run(self.family, tr.A, tr.B, cm='150')
        fits = res.data['dna']['fits']
        self.assertGreater(len(fits), 5)
        self.assertIn(f'and {len(fits) - 5} more', out)
        self.assertIn('fha relate --cm 150', out)

    def test_lookup_one_fit_is_not_several(self):
        bare = Path(tempfile.mkdtemp())
        (bare / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        res, _, out, _ = _run(bare, None, None, cm='3600')
        self.assertEqual(len(res.data['fits']), 1)
        self.assertNotIn('several', out)
        self.assertIn('one relationship', out)


class MisfitCauseTests(unittest.TestCase):
    """When the amount is outside the recorded tie, the suggested causes fit
    which way it misses: a half relationship is offered only for a full tie
    with too little DNA, never for half-siblings or for too much."""

    def _text(self, gens_a, gens_b, half, cm):
        dna = relate._dna_answer({'gens_a': gens_a, 'gens_b': gens_b, 'half': half}, 1, cm)
        self.assertFalse(dna['fits_recorded'])
        return ' '.join(relate._dna_lines(dna)).lower()

    def test_full_tie_too_little_suggests_half(self):
        self.assertIn('a half relationship', self._text(2, 2, False, 200))

    def test_half_tie_too_little_does_not_suggest_half(self):
        out = self._text(1, 1, True, 450)
        self.assertNotIn('a half relationship', out)
        self.assertIn('wrong or missing parent', out)

    def test_too_much_does_not_suggest_half(self):
        out = self._text(1, 1, None, 3600)
        self.assertNotIn('a half relationship', out)
        self.assertIn('closer tie', out)

    def test_lineal_tie_does_not_suggest_half(self):
        self.assertNotIn('a half relationship', self._text(0, 2, False, 500))

    def test_one_other_fit_does_not_say_which(self):
        from unittest import mock
        with mock.patch.object(relate, 'shared_cm_table', return_value=_table_without('great_grandparent')):
            dna = relate._dna_answer({'gens_a': 0, 'gens_b': 3, 'half': False}, 1, 3600)
            out = ' '.join(relate._dna_lines(dna))
        self.assertEqual(len(dna['fits']), 1)
        self.assertNotIn('decide which', out)


def _build_far_route() -> tuple[Path, str, str]:
    """First cousins A and B who are also third cousins through their
    mothers: a second route that adds only a little DNA."""
    ids = iter(f'P-9a9a9a9a{i:02x}' for i in range(1, 40))
    G, A1, B1, A, B, T, t1, t2, u1, u2, MA, MB = (next(ids) for _ in range(12))
    k = iter(range(1, 99))
    claims = ''
    for child, parents in ((A1, [G]), (B1, [G]), (t1, [T]), (t2, [T]), (u1, [t1]),
                           (u2, [t2]), (MA, [u1]), (MB, [u2]), (A, [A1, MA]), (B, [B1, MB])):
        claims += tr._child_of(f'C-a0000000{next(k):02d}', child, parents)
    sex = {p: 'M' for p in (G, A1, B1, A, B, T, t1, t2, u1, u2)}
    sex.update({MA: 'F', MB: 'F'})
    return tr._write_archive(claims, sex, {p: f'Pers {i}' for i, p in enumerate(sex)}), A, B


class FarRouteTests(unittest.TestCase):
    """A distant second route cannot explain far too much DNA."""

    def test_far_second_route_does_not_excuse_too_much(self):
        root, a, b = _build_far_route()
        res, _, out, _ = _run(root, a, b, cm='2500')
        self.assertTrue(res.data['dna']['more_than_one_route'])
        self.assertFalse(res.data['dna']['fits_recorded'])
        self.assertNotIn('which can mean more DNA', out)
        self.assertIn('wrong parent', out)

    def test_second_route_does_not_excuse_too_little(self):
        res, _, out, _ = _run(_build_double(), C3, C4, cm='100')
        self.assertFalse(res.data['dna']['fits_recorded'])
        self.assertNotIn('which can mean more DNA', out)

    def test_far_second_route_within_sum_is_unknown(self):
        root, a, b = _build_far_route()
        res, _, out, _ = _run(root, a, b, cm='1420')
        self.assertIsNone(res.data['dna']['fits_recorded'])
        self.assertIn('which can mean more DNA', out)


if __name__ == '__main__':
    unittest.main()
