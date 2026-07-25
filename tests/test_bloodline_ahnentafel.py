"""
test_bloodline_ahnentafel.py - bloodline-aware Ahnentafel numbering + bracket
nature marks (SPEC §12.2, chunk 02).

The pedigree NUMBERING follows only genetic parent edges; social/legal parents
(adoptive, step, …) are shown in the couple-folder bracket lists - marked
`(adopted)` etc. - but never numbered. An all-biological archive numbers and
brackets exactly as before (back-compat). Both backends are exercised: lint's
in-memory registry and views' SQLite index, which must agree.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import lint
import views
import index as index_mod
from _lib import (
    load_fha_yaml,
    is_genetic_parent_subtype,
    nonbirth_bracket_label,
    format_bracket_child,
)

KID = 'P-aaaaaaaaaa'
BIOP = 'P-bbbbbbbbbb'
BIOM = 'P-cccccccccc'
ADOP = 'P-dddddddddd'
ADOM = 'P-eeeeeeeeee'
SID = 'S-ffffffffff'


def _ptext(pid: str, name: str, sex: str = 'U') -> str:
    return (f'---\nid: {pid}\nname: {name}\nsex: {sex}\nliving: false\n'
            f'tier: stub\n---\n\n# {name}\n\n## Biography\n\nx\n')


def _rel_claim(cid: str, child: str, parents: list[str], subtype: str) -> str:
    plist = ', '.join(parents)
    persons = ', '.join([child] + parents)
    return (
        f'- value: "{child} child of {plist}"\n'
        f'  id: {cid}\n  type: relationship\n  subtype: {subtype}\n'
        f'  persons: [{persons}]\n  roles:\n'
        f'    child: {child}\n    parent: [{plist}]\n'
        f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
        f'  information: primary\n  evidence: direct\n  notes: x.\n'
    )


def _source(sid: str, claims_yaml: str) -> str:
    return (f'---\nid: {sid}\ntitle: Rel\nsource_type: other\n---\n\n'
            f'## Claims\n```yaml\n{claims_yaml}```\n')


def _build(files: dict[str, str], root_person: str | None = None) -> Path:
    root = Path(tempfile.mkdtemp())
    (root / 'people' / 'stubs').mkdir(parents=True)
    (root / 'sources' / 'notes').mkdir(parents=True)
    cfg = ''
    if root_person:
        cfg += f'root_person: {root_person}\n'
    cfg += 'roots:\n  documents: documents\n'
    (root / 'fha.yaml').write_text(cfg, encoding='utf-8')
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
    return root


def _open(root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(root / '.cache' / 'index.sqlite'))
    conn.row_factory = sqlite3.Row
    return conn


class NatureHelperTests(unittest.TestCase):
    def test_unset_legacy_unknown_default_to_genetic(self) -> None:
        for s in ('biological', 'surrogate-genetic', 'donor-sperm', 'donor-egg',
                  '', None, 'child-of', 'spouse-of', 'mystery-nature'):
            self.assertTrue(is_genetic_parent_subtype(s), s)

    def test_social_legal_are_not_genetic_and_have_labels(self) -> None:
        for s in ('adoptive', 'step', 'foster', 'guardian',
                  'surrogate-gestational', 'social'):
            self.assertFalse(is_genetic_parent_subtype(s), s)
            self.assertIsNotNone(nonbirth_bracket_label(s), s)

    def test_labels_and_formatting(self) -> None:
        self.assertIsNone(nonbirth_bracket_label('biological'))
        self.assertEqual(nonbirth_bracket_label('adoptive'), 'adopted')
        self.assertEqual(format_bracket_child('Ruth', None), 'Ruth')
        self.assertEqual(format_bracket_child('Ruth', 'adopted'), 'Ruth (adopted)')


class GeneticNumberingTests(unittest.TestCase):
    """Kid has both biological and adoptive parents; only the genetic pair numbers."""

    def _arc(self) -> Path:
        claims = (_rel_claim('C-1111111111', KID, [BIOP, BIOM], 'biological')
                  + _rel_claim('C-2222222222', KID, [ADOP, ADOM], 'adoptive'))
        files = {
            f'people/stubs/kid__ann_{KID}.md': _ptext(KID, 'Ann Kid'),
            f'people/stubs/bio__pa_{BIOP}.md': _ptext(BIOP, 'Pa Bio', 'M'),
            f'people/stubs/bio__ma_{BIOM}.md': _ptext(BIOM, 'Ma Bio', 'F'),
            f'people/stubs/ado__pa_{ADOP}.md': _ptext(ADOP, 'Pa Ado', 'M'),
            f'people/stubs/ado__ma_{ADOM}.md': _ptext(ADOM, 'Ma Ado', 'F'),
            f'sources/notes/{SID.lower()}.md': _source(SID, claims),
        }
        return _build(files, root_person=KID)

    def test_lint_genetic_children_excludes_adoptive(self) -> None:
        root = self._arc()
        _f, reg = lint._run_lint_core(root, load_fha_yaml(root))
        all_edges = lint._build_children_of(reg)
        genetic = lint._build_children_of(reg, genetic_only=True)
        # Both parent pairs are present in the unfiltered view (brackets show all).
        self.assertEqual(all_edges[BIOP.lower()], {KID.lower()})
        self.assertEqual(all_edges[ADOP.lower()], {KID.lower()})
        # Only the genetic pair survives the numbering filter.
        self.assertEqual(genetic[BIOP.lower()], {KID.lower()})
        self.assertNotIn(ADOP.lower(), genetic)
        self.assertNotIn(ADOM.lower(), genetic)

    def test_lint_ahnentafel_numbers_only_genetic(self) -> None:
        root = self._arc()
        _f, reg = lint._run_lint_core(root, load_fha_yaml(root))
        genetic = lint._build_children_of(reg, genetic_only=True)
        pos = lint._build_ahnentafel_lint(KID.lower(), genetic, reg)
        self.assertEqual(pos.get(BIOP.lower()), 2)
        self.assertEqual(pos.get(BIOM.lower()), 3)
        self.assertNotIn(ADOP.lower(), pos)
        self.assertNotIn(ADOM.lower(), pos)

    def test_views_ahnentafel_numbers_only_genetic(self) -> None:
        root = self._arc()
        index_mod.build_index(root, load_fha_yaml(root))
        conn = _open(root)
        try:
            pos = views._build_ahnentafel_map(conn, KID.lower())
        finally:
            conn.close()
        self.assertEqual(pos.get(BIOP.lower()), 2)
        self.assertEqual(pos.get(BIOM.lower()), 3)
        self.assertNotIn(ADOP.lower(), pos)
        self.assertNotIn(ADOM.lower(), pos)


class MultiContributorTests(unittest.TestCase):
    """Assisted reproduction with THREE genetic contributors: a donor-sperm
    father and two genetic mothers (donor-egg + surrogate-genetic). The
    two-slot Ahnentafel model must seat the sperm contributor in the father
    slot (2n) and one genetic mother - the lowest P-id, deterministically -
    in the mother slot (2n+1). It must never take the first two unordered SQL
    rows, which could seat two mothers in both slots and drop the father
    (SPEC 12.2, TOOLING 7)."""

    SPERM = 'P-mmmmmmmmmm'   # sex M - the only male contributor
    EGG = 'P-gggggggggg'     # sex F - lower P-id of the two mothers -> wins slot 3
    SURR = 'P-hhhhhhhhhh'    # sex F - higher P-id -> bracketed, unnumbered

    def _arc(self, reversed_order: bool = False) -> Path:
        edges = [
            ('C-3333333333', [self.SPERM], 'donor-sperm'),
            ('C-4444444444', [self.EGG], 'donor-egg'),
            ('C-5555555555', [self.SURR], 'surrogate-genetic'),
        ]
        if reversed_order:
            edges = list(reversed(edges))
        claims = ''.join(_rel_claim(cid, KID, ps, sub) for cid, ps, sub in edges)
        files = {
            f'people/stubs/kid__ann_{KID}.md': _ptext(KID, 'Ann Kid'),
            f'people/stubs/spr__pa_{self.SPERM}.md': _ptext(self.SPERM, 'Pa Sperm', 'M'),
            f'people/stubs/egg__ma_{self.EGG}.md': _ptext(self.EGG, 'Ma Egg', 'F'),
            f'people/stubs/sur__ma_{self.SURR}.md': _ptext(self.SURR, 'Ma Surr', 'F'),
            f'sources/notes/{SID.lower()}.md': _source(SID, claims),
        }
        return _build(files, root_person=KID)

    def _positions(self, root: Path) -> dict:
        index_mod.build_index(root, load_fha_yaml(root))
        conn = _open(root)
        try:
            return views._build_ahnentafel_map(conn, KID.lower())
        finally:
            conn.close()

    def test_sperm_takes_father_slot_lowest_mother_takes_mother_slot(self) -> None:
        pos = self._positions(self._arc())
        # Father slot (2) is the sole male genetic contributor - never dropped.
        self.assertEqual(pos.get(self.SPERM.lower()), 2)
        # Mother slot (3) is the lowest-P-id of the two genetic mothers.
        self.assertEqual(pos.get(self.EGG.lower()), 3)
        # The third contributor takes no numbered slot (shown in brackets).
        self.assertNotIn(self.SURR.lower(), pos)

    def test_selection_is_independent_of_claim_order(self) -> None:
        # The same three edges written in opposite order must number identically
        # - the rule ranks by (sex, P-id), never by SQL row order.
        forward = self._positions(self._arc(reversed_order=False))
        backward = self._positions(self._arc(reversed_order=True))
        for who in (self.SPERM.lower(), self.EGG.lower()):
            self.assertEqual(forward.get(who), backward.get(who), who)
        self.assertNotIn(self.SURR.lower(), backward)


class BracketMarkTests(unittest.TestCase):
    """A couple folder with a biological child and an adopted child: both shown,
    the adopted one marked, in both lint (W103) and views (W103)."""

    FOLDER = 'people/040 Pa Bio + Ma Bio'

    def _arc(self) -> Path:
        claims = (_rel_claim('C-1111111111', KID, [BIOP, BIOM], 'biological')
                  + _rel_claim('C-2222222222', ADOP, [BIOP, BIOM], 'adoptive'))
        files = {
            f'{self.FOLDER}/bio__pa_{BIOP}.md': _ptext(BIOP, 'Pa Bio', 'M'),
            f'{self.FOLDER}/bio__ma_{BIOM}.md': _ptext(BIOM, 'Ma Bio', 'F'),
            f'people/stubs/kid__ann_{KID}.md': _ptext(KID, 'Ann Kid'),
            f'people/stubs/ado__rae_{ADOP}.md': _ptext(ADOP, 'Rae Ado'),
            f'sources/notes/{SID.lower()}.md': _source(SID, claims),
        }
        return _build(files)

    def test_lint_w103_marks_adopted_child(self) -> None:
        root = self._arc()
        findings, _ = lint._run_lint_core(root, load_fha_yaml(root))
        w103 = [f for f in findings if f.code == 'W103']
        self.assertTrue(w103)
        msg = w103[0].message
        self.assertIn('Rae (adopted)', msg)   # adopted child marked
        self.assertIn('Ann', msg)             # biological child bare
        self.assertNotIn('Ann (', msg)        # ...and NOT marked

    def test_views_w103_marks_adopted_child(self) -> None:
        root = self._arc()
        index_mod.build_index(root, load_fha_yaml(root))
        conn = _open(root)
        try:
            issues = views._check_w103_brackets(conn, root)
        finally:
            conn.close()
        new_names = ' | '.join(i['new_name'] for i in issues)
        self.assertIn('Rae (adopted)', new_names)
        self.assertIn('Ann', new_names)


class BackCompatTests(unittest.TestCase):
    """An all-biological couple folder produces a bare bracket list - no marks,
    identical to pre-bloodline behavior."""

    FOLDER = 'people/040 Pa Bio + Ma Bio'

    def test_all_biological_brackets_are_bare(self) -> None:
        claims = (_rel_claim('C-1111111111', KID, [BIOP, BIOM], 'biological')
                  + _rel_claim('C-2222222222', ADOP, [BIOP, BIOM], ''))  # unset → genetic
        files = {
            f'{self.FOLDER}/bio__pa_{BIOP}.md': _ptext(BIOP, 'Pa Bio', 'M'),
            f'{self.FOLDER}/bio__ma_{BIOM}.md': _ptext(BIOM, 'Ma Bio', 'F'),
            f'people/stubs/kid__ann_{KID}.md': _ptext(KID, 'Ann Kid'),
            f'people/stubs/ado__rae_{ADOP}.md': _ptext(ADOP, 'Rae Kid'),
            f'sources/notes/{SID.lower()}.md': _source(SID, claims),
        }
        root = _build(files)
        findings, _ = lint._run_lint_core(root, load_fha_yaml(root))
        w103 = [f for f in findings if f.code == 'W103']
        self.assertTrue(w103)
        self.assertNotIn('(', w103[0].message.split('->')[1])  # no nature marks


if __name__ == '__main__':
    unittest.main()
