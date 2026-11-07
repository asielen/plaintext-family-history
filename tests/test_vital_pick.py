"""
test_vital_pick.py - #201: which accepted claim becomes a person's life date.

A person often has several accepted claims for one vital that disagree: a
death certificate's "19 Jan 1941" beside a relative's memory of the funeral,
"about 1940". The headline date used to be whichever claim id sorted first.
Claim ids are random, so the family memory could win, on the person page, on
every chart node and in `fha views tree`, with nothing to say so. GEDCOM took
the earliest `date_min`, which always favoured the vaguest date.

One rule now, `_lib.vital_pick_key`, used by every surface: a dated claim
before an undated one, then the narrowest date range, then confidence, then
claim id.

Synthetic tmp archive only - the real archive is never a test bed.
"""

import contextlib
import importlib.util
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import gedcom  # noqa: E402
import views  # noqa: E402
from _lib import vital_pick_key  # noqa: E402
from index import build_index  # noqa: E402

# tools/site.py shares its name with the stdlib `site` module, which Python
# has already imported, so it is loaded by path (as tests/test_site.py does).
_spec = importlib.util.spec_from_file_location('fha_site', ROOT / 'tools' / 'site.py')
site_mod = importlib.util.module_from_spec(_spec)
sys.modules['fha_site'] = site_mod
_spec.loader.exec_module(site_mod)

PID = 'P-aaaaaaaaaa'

PERSON = """---
id: P-aaaaaaaaaa
name: Ann Doe
sex: F
living: false
tier: stub
created: 2026-11-06
---
"""


def _source(sid: str, title: str, claims_yaml: str) -> str:
    return (
        f'---\nid: {sid}\ntitle: {title}\nsource_type: vital-record\n'
        f'source_class: original\npeople:\n  - "[[{PID}|Ann Doe]]"\n'
        f'created: 2026-11-06\n---\n\n## Claims\n```yaml\n{claims_yaml}```\n'
    )


# The lowest claim id carries no date at all, the next the vague memory, the
# highest the certificate: under the old `ORDER BY c.id` pick the undated or
# vague claim always won.
MEMORY = _source('S-2222222222', 'Family memory of the funeral', """\
- value: "Ann died, no date remembered"
  id: C-0000000000
  type: death
  persons: [P-aaaaaaaaaa]
  status: accepted
  confidence: low
- value: "Ann died about 1940"
  id: C-aaaaaaaaaa
  type: death
  persons: [P-aaaaaaaaaa]
  date: 1940~
  status: accepted
  confidence: medium
- value: "Ann born 1880, as the family tells it"
  id: C-bbbbbbbbbb
  type: birth
  persons: [P-aaaaaaaaaa]
  date: 1880
  status: accepted
  confidence: low
""")

CERTIFICATE = _source('S-1111111111', 'Death certificate of Ann Doe', """\
- value: "Ann Doe died 19 January 1941"
  id: C-zzzzzzzzzz
  type: death
  persons: [P-aaaaaaaaaa]
  date: 1941-01-19
  status: accepted
  confidence: high
- value: "Ann Doe born 1880, per the certificate"
  id: C-yyyyyyyyyy
  type: birth
  persons: [P-aaaaaaaaaa]
  date: 1880
  status: accepted
  confidence: high
""")


class VitalPickKeyTests(unittest.TestCase):
    """The rule itself, on plain mappings."""

    @staticmethod
    def _c(cid, date_min, date_max, confidence):
        return {'id': cid, 'date_min': date_min, 'date_max': date_max,
                'confidence': confidence}

    def _best(self, *claims):
        return sorted(claims, key=vital_pick_key)[0]['id']

    def test_an_exact_day_beats_an_about_year(self):
        self.assertEqual(self._best(
            self._c('C-a', '1939-01-01', '1941-12-31', 'medium'),
            self._c('C-z', '1941-01-19', '1941-01-19', 'high')), 'C-z')

    def test_exactness_beats_confidence(self):
        # Confidence first was tried on a real archive and mostly traded an
        # exact month for a year range.
        self.assertEqual(self._best(
            self._c('C-a', '1824-01-01', '1825-12-31', 'high'),
            self._c('C-z', '1825-06-01', '1825-06-30', 'medium')), 'C-z')

    def test_confidence_breaks_an_equal_span(self):
        self.assertEqual(self._best(
            self._c('C-a', '1880-01-01', '1880-12-31', 'low'),
            self._c('C-z', '1880-01-01', '1880-12-31', 'high')), 'C-z')

    def test_an_open_before_date_still_beats_no_date(self):
        # [..1900] is a very wide range, but it is a date.
        self.assertEqual(self._best(
            self._c('C-a', None, None, 'high'),
            self._c('C-z', '0001-01-01', '1900-12-31', 'low')), 'C-z')

    def test_an_exact_year_before_1000_beats_its_decade(self):
        # #207 review: '0999' used to get bounds '999-01-01', which the span
        # parser rejected, so the exact year read as undated and the decade
        # '099X' won even at lower confidence.
        from _lib import edtf_bounds

        def dated(cid, edtf, confidence):
            lo, hi = edtf_bounds(edtf)
            return self._c(cid, lo, hi, confidence)
        self.assertEqual(self._best(
            dated('C-a', '099X', 'high'), dated('C-z', '0999', 'low')), 'C-z')

    def _dated(self, cid, edtf, confidence):
        from _lib import edtf_bounds
        lo, hi = edtf_bounds(edtf)
        return self._c(cid, lo, hi, confidence)

    def test_a_leap_year_does_not_outrank_confidence(self):
        # #207 audit: by raw days 1881 (364) was "narrower" than leap-year
        # 1880 (365), so a low-confidence memory beat the certificate.
        self.assertEqual(self._best(
            self._dated('C-a', '1880', 'high'), self._dated('C-z', '1881', 'low')), 'C-a')

    def test_month_length_does_not_outrank_confidence(self):
        self.assertEqual(self._best(
            self._dated('C-a', '1925-03', 'high'), self._dated('C-z', '1925-02', 'low')), 'C-a')

    def test_two_about_years_go_by_confidence(self):
        self.assertEqual(self._best(
            self._dated('C-a', '1880~', 'high'), self._dated('C-z', '1882~', 'low')), 'C-a')

    def test_a_backwards_interval_does_not_win(self):
        # A typing slip '1950/1940' has a negative width; it must not beat
        # the certificate's exact day by looking narrower than zero.
        self.assertEqual(self._best(
            self._dated('C-a', '1950/1940', 'low'),
            self._dated('C-z', '1941-01-19', 'high')), 'C-z')

    def test_an_index_built_before_padding_still_ranks(self):
        # An index written by the old tools still holds '999-01-01'.
        self.assertEqual(self._best(
            self._c('C-a', '0990-01-01', '0999-12-31', 'high'),
            self._c('C-z', '999-01-01', '999-12-31', 'low')), 'C-z')

    def test_claim_id_is_the_last_tie_break(self):
        self.assertEqual(self._best(
            self._c('C-z', '1880-01-01', '1880-12-31', 'high'),
            self._c('C-a', '1880-01-01', '1880-12-31', 'high')), 'C-a')


class EarlyYearBoundsTests(unittest.TestCase):
    """#207 review: date bounds are ISO strings compared as text (timeline
    order, `fha xref` overlap), so a year must always be four digits."""

    def test_years_before_1000_are_zero_padded(self):
        from _lib import edtf_bounds
        self.assertEqual(edtf_bounds('0999'), ('0999-01-01', '0999-12-31'))
        self.assertEqual(edtf_bounds('0999~'), ('0998-01-01', '1000-12-31'))
        self.assertEqual(edtf_bounds('099X'), ('0990-01-01', '0999-12-31'))
        self.assertEqual(edtf_bounds('0850-03'), ('0850-03-01', '0850-03-31'))
        self.assertEqual(edtf_bounds('0850-03-07'), ('0850-03-07', '0850-03-07'))
        self.assertEqual(edtf_bounds('0000~')[0], '0000-01-01')

    def test_text_order_is_date_order_across_year_1000(self):
        from _lib import edtf_bounds
        self.assertLess(edtf_bounds('0999')[1], edtf_bounds('1000')[0])


class EverySurfaceAgreesTests(unittest.TestCase):
    """End to end: one archive, every surface that shows a life date."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        (cls.root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
        (cls.root / 'people' / 'stubs').mkdir(parents=True)
        (cls.root / 'people' / 'stubs' / 'doe__ann_P-aaaaaaaaaa.md').write_text(
            PERSON, encoding='utf-8')
        (cls.root / 'sources' / 'vital-record').mkdir(parents=True)
        (cls.root / 'sources' / 'vital-record' / 'memory_S-2222222222.md').write_text(
            MEMORY, encoding='utf-8')
        (cls.root / 'sources' / 'vital-record' / 'certificate_S-1111111111.md').write_text(
            CERTIFICATE, encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            result = build_index(cls.root, {'roots': {'documents': 'documents'}})
        assert result.ok, result
        cls.conn = sqlite3.connect(str(cls.root / '.cache' / 'index.sqlite'))
        cls.conn.row_factory = sqlite3.Row
        cls.pid = cls.conn.execute('SELECT id FROM persons').fetchone()['id']

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()
        cls._tmp.cleanup()

    def _builder(self):
        builder = site_mod._SiteBuilder(
            self.conn, self.root, {}, self.root / '.cache' / 'site', linked=True)
        builder.prepare()
        return builder

    def test_the_fixture_really_has_three_accepted_deaths(self):
        n = self.conn.execute(
            "SELECT COUNT(*) FROM claims WHERE type = 'death' AND status = 'accepted'"
        ).fetchone()[0]
        self.assertEqual(n, 3)

    def test_person_page_summary(self):
        builder = self._builder()
        page_dir = self.root / '.cache' / 'site' / 'persons'
        rows = {r['label']: r for r in builder._person_summary(self.pid, page_dir)}
        died = next(r for label, r in rows.items() if label.startswith('Died'))
        born = next(r for label, r in rows.items() if label.startswith('Born'))
        self.assertEqual(died['value'], '1941-01-19')
        # Same year on both birth claims: the certificate's high confidence wins.
        self.assertEqual(born['confidence'], 'high')

    def test_site_chart_node(self):
        self.assertEqual(self._builder()._person_vitals(self.pid)['death'], '1941-01-19')

    def test_views_tree_node(self):
        nodes = views._build_nodes_bulk(self.conn, [self.pid])
        self.assertEqual(nodes[self.pid]['vitals']['death'], '1941-01-19')

    def test_gedcom_deat(self):
        rows = gedcom._load_vitals(self.conn, {self.pid})
        self.assertEqual(rows[(self.pid, 'death')]['date_edtf'], '1941-01-19')
        self.assertEqual(rows[(self.pid, 'birth')]['id'].lower(), 'c-yyyyyyyyyy')

    def test_gedcom_file_carries_the_certificate_date(self):
        result = gedcom.run_gedcom(self.root, None, all_persons=True)
        self.assertIn('19 JAN 1941', result['text'])
        self.assertNotIn('ABT 1940', result['text'])


if __name__ == '__main__':
    unittest.main()
