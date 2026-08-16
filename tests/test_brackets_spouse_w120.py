"""
test_brackets_spouse_w120.py - the two 2026-07-26 live-usage additions to the
bracket/Ahnentafel machinery (SPEC §12.2, TOOLING §7):

1. The `+ second spouse` half of a couple-folder name: W103 (both backends)
   proposes ADDING the missing partner to a folder that names only one - the
   shared `_lib.spouse_extended_base` rule - never rewriting an existing
   `+ …` half and never guessing when the base name matches neither/both
   partners.

2. W120: a lone linked parent with no recorded `sex:` takes the father (even)
   slot by DEFAULT, so the derived folder numbers above them look confirmed
   while being a guess. Both backends report it; two resolved parents with
   unset sex stay silent (the genuine same-sex/unknown tie-break case).

Both backends are exercised - lint's in-memory registry and views' SQLite
index - which must agree. Fixtures only.
"""

import contextlib
import io
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
    EXIT_WARNINGS,
    build_ahnentafel_map,
    load_fha_yaml,
    spouse_extended_base,
)

KID = 'P-3aaaaaaaaa'
KID2 = 'P-3bbbbbbbbb'
ROB = 'P-3ccccccccc'
JEA = 'P-3ddddddddd'
MA = 'P-3eeeeeeeee'
PA = 'P-3fffffffff'
SID = 'S-3aaaaaaaaa'


def _ptext(pid: str, name: str, sex: str = 'U', tier: str = 'stub') -> str:
    return (f'---\nid: {pid}\nname: {name}\nsex: {sex}\nliving: false\n'
            f'tier: {tier}\n---\n\n# {name}\n\n## Biography\n\nx\n')


def _rel_claim(cid: str, child: str, parents: list[str]) -> str:
    plist = ', '.join(parents)
    persons = ', '.join([child] + parents)
    return (
        f'- value: "{child} child of {plist}"\n'
        f'  id: {cid}\n  type: relationship\n  subtype: biological\n'
        f'  persons: [{persons}]\n  roles:\n'
        f'    child: {child}\n    parent: [{plist}]\n'
        f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
        f'  information: primary\n  evidence: direct\n  notes: x.\n'
    )


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


def _views_w103(root: Path) -> list[dict]:
    index_mod.build_index(root, load_fha_yaml(root))
    conn = _open(root)
    try:
        return views._check_w103_brackets(conn, root)
    finally:
        conn.close()


def _run_brackets(root: Path, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        res = views.run_brackets(root, **kwargs)
    return res, out.getvalue(), err.getvalue()


class SpouseExtendedBaseUnitTests(unittest.TestCase):
    """The shared add-only rule, exercised directly."""

    NAMES = {'p-1': 'Robert E. Church', 'p-2': 'Jeanne Stemler'}

    def test_appends_the_other_partner_on_an_exact_match(self) -> None:
        base, other = spouse_extended_base(
            '004 Robert E. Church', ['p-1', 'p-2'], self.NAMES)
        self.assertEqual(base, '004 Robert E. Church + Jeanne Stemler')
        self.assertEqual(other, 'Jeanne Stemler')

    def test_match_is_case_and_whitespace_insensitive(self) -> None:
        base, other = spouse_extended_base(
            '004 robert  e. church', ['p-1', 'p-2'], self.NAMES)
        self.assertEqual(base, '004 robert  e. church + Jeanne Stemler')
        self.assertEqual(other, 'Jeanne Stemler')

    def test_existing_plus_half_is_never_touched(self) -> None:
        for written in ('004 Robert E. Church + Jeannie',
                        '004 Robert E. Church + (second wife)'):
            base, other = spouse_extended_base(
                written, ['p-1', 'p-2'], self.NAMES)
            self.assertEqual(base, written)
            self.assertIsNone(other)

    def test_nonmatching_base_is_left_alone(self) -> None:
        base, other = spouse_extended_base(
            '004 Church Family', ['p-1', 'p-2'], self.NAMES)
        self.assertEqual(base, '004 Church Family')
        self.assertIsNone(other)

    def test_requires_exactly_two_partners(self) -> None:
        for ids in (['p-1'], ['p-1', 'p-2', 'p-3']):
            names = {**self.NAMES, 'p-3': 'Third Person'}
            base, other = spouse_extended_base(
                '004 Robert E. Church', ids, names)
            self.assertEqual(base, '004 Robert E. Church')
            self.assertIsNone(other)

    def test_partner_without_a_name_is_never_written(self) -> None:
        # A partner whose display name fell back to their bare P-id must not
        # end up in a folder name (folder names are for humans).
        names = {'p-1': 'Robert E. Church', 'p-2': 'p-2'}
        base, other = spouse_extended_base(
            '004 Robert E. Church', ['p-1', 'p-2'], names)
        self.assertEqual(base, '004 Robert E. Church')
        self.assertIsNone(other)

    def test_both_partners_same_name_is_ambiguous_and_skipped(self) -> None:
        names = {'p-1': 'Jo Smith', 'p-2': 'Jo Smith'}
        base, other = spouse_extended_base(
            '004 Jo Smith', ['p-1', 'p-2'], names)
        self.assertEqual(base, '004 Jo Smith')
        self.assertIsNone(other)

    def test_placeholder_names_are_never_written(self) -> None:
        # `fha stubs` mints an unnamed reference as `name: unknown`; the index
        # stores 'unknown' for a record with no name and 'None' for a bare
        # `name:` key. None of those belong in a folder name - and W103's
        # add-only rule would never revisit `+ unknown` once the real name
        # lands, so the folder would carry the placeholder forever.
        for placeholder in ('unknown', 'Unknown', 'None', '', '  ', 'null'):
            names = {'p-1': 'Robert E. Church', 'p-2': placeholder}
            base, other = spouse_extended_base(
                '004 Robert E. Church', ['p-1', 'p-2'], names)
            self.assertEqual(base, '004 Robert E. Church', repr(placeholder))
            self.assertIsNone(other, repr(placeholder))


class SpouseFolderNameTests(unittest.TestCase):
    """The live-archive case: both partners curated in the couple folder, the
    folder named after only one of them - both backends propose the add."""

    FOLDER = 'people/004 Robert E. Church [Lisa + Robert]'

    def _files(self, folder: str | None = None) -> dict[str, str]:
        folder = folder or self.FOLDER
        claims = (_rel_claim('C-3111111111', KID, [ROB, JEA])
                  + _rel_claim('C-3222222222', KID2, [ROB, JEA]))
        return {
            f'{folder}/church__robert_{ROB}.md':
                _ptext(ROB, 'Robert E. Church', 'M', 'curated'),
            f'{folder}/stemler__jeanne_{JEA}.md':
                _ptext(JEA, 'Jeanne Stemler', 'F', 'curated'),
            f'people/stubs/church__lisa_{KID}.md': _ptext(KID, 'Lisa Church', 'F'),
            f'people/stubs/church__robert_jr_{KID2}.md': _ptext(KID2, 'Robert Church'),
            f'sources/notes/{SID.lower()}.md': (
                f'---\nid: {SID}\ntitle: Rel\nsource_type: other\n---\n\n'
                f'## Claims\n```yaml\n{claims}```\n'),
        }

    def test_views_proposes_adding_the_second_spouse(self) -> None:
        root = _build(self._files())
        issues = _views_w103(root)
        self.assertEqual(len(issues), 1)
        self.assertEqual(
            issues[0]['new_name'],
            '004 Robert E. Church + Jeanne Stemler [Lisa + Robert]')
        self.assertIn('names only one partner - add Jeanne Stemler',
                      issues[0]['msg'])
        # The bracket list was already correct: it must be carried over
        # byte-for-byte, not re-derived (and not reported as stale).
        self.assertNotIn('stale bracket list', issues[0]['msg'])

    def test_views_leaves_a_two_partner_name_alone(self) -> None:
        root = _build(self._files(
            'people/004 Robert E. Church + Jeanne Stemler [Lisa + Robert]'))
        self.assertEqual(_views_w103(root), [])

    def test_views_never_guesses_at_a_hand_crafted_base(self) -> None:
        root = _build(self._files('people/004 Church Family [Lisa + Robert]'))
        self.assertEqual(_views_w103(root), [])

    def test_lint_mirrors_the_same_proposal(self) -> None:
        root = _build(self._files())
        findings, _ = lint._run_lint_core(root, load_fha_yaml(root))
        w103 = [f for f in findings if f.code == 'W103']
        self.assertEqual(len(w103), 1)
        self.assertIn('names only one partner - add Jeanne Stemler',
                      w103[0].message)
        self.assertIn('fha views brackets --fix', w103[0].message)

    def test_lint_leaves_a_two_partner_name_alone(self) -> None:
        root = _build(self._files(
            'people/004 Robert E. Church + Jeanne Stemler [Lisa + Robert]'))
        findings, _ = lint._run_lint_core(root, load_fha_yaml(root))
        self.assertEqual([f for f in findings if f.code == 'W103'], [])

    def test_spouse_add_and_stale_bracket_combine_in_one_rename(self) -> None:
        root = _build(self._files('people/004 Robert E. Church [Lisa]'))
        issues = _views_w103(root)
        self.assertEqual(len(issues), 1)
        self.assertEqual(
            issues[0]['new_name'],
            '004 Robert E. Church + Jeanne Stemler [Lisa + Robert]')
        self.assertIn('names only one partner', issues[0]['msg'])
        self.assertIn('stale bracket list', issues[0]['msg'])


class W120SexGapTests(unittest.TestCase):
    """A lone linked parent with no recorded sex: takes the even slot by
    default - W120 in both backends; silent once sex: is recorded, and silent
    for a genuinely two-parent unknown-sex couple (the tie-break case)."""

    def _files(self, ma_sex: str, two_parents: bool = False) -> dict[str, str]:
        parents = [PA, MA] if two_parents else [MA]
        claims = _rel_claim('C-3333333333', KID, parents)
        files = {
            f'people/stubs/solo__kid_{KID}.md': _ptext(KID, 'Kid Solo', 'F'),
            f'people/stubs/solo__ma_{MA}.md': _ptext(MA, 'Ma Solo', ma_sex),
            f'sources/notes/{SID.lower()}.md': (
                f'---\nid: {SID}\ntitle: Rel\nsource_type: other\n---\n\n'
                f'## Claims\n```yaml\n{claims}```\n'),
        }
        if two_parents:
            files[f'people/stubs/solo__pa_{PA}.md'] = _ptext(PA, 'Pa Solo', 'U')
        return files

    def test_lib_map_collects_the_gap(self) -> None:
        root = _build(self._files('U'), root_person=KID)
        index_mod.build_index(root, load_fha_yaml(root))
        conn = _open(root)
        try:
            gaps: list[dict] = []
            pos = build_ahnentafel_map(conn, KID.lower(), gaps)
        finally:
            conn.close()
        self.assertEqual(pos.get(MA.lower()), 2)   # defaulted to the even slot
        self.assertEqual(gaps, [{'pid': MA.lower(), 'pos': 2, 'sex': 'U'}])

    def test_explicit_intersex_or_unknown_is_a_recorded_fact_not_a_gap(self) -> None:
        # SPEC §9's vocabulary is M | F | intersex | unknown. An explicitly
        # recorded intersex/unknown is a fact the human already stated: the
        # deterministic tie-break is the designed behaviour for it, and a
        # permanent W120 saying 'no sex: recorded ... record M or F' against
        # a correct record would only teach the human to overwrite it. Both
        # twins must agree.
        for sex in ('intersex', 'unknown'):
            root = _build(self._files(sex), root_person=KID)
            index_mod.build_index(root, load_fha_yaml(root))
            conn = _open(root)
            try:
                gaps: list[dict] = []
                pos = build_ahnentafel_map(conn, KID.lower(), gaps)
            finally:
                conn.close()
            self.assertEqual(pos.get(MA.lower()), 2, sex)   # still the even slot
            self.assertEqual(gaps, [], sex)
            res, _out, _err = _run_brackets(root)
            self.assertEqual(res.data['w120'], 0, sex)
            findings, _ = lint._run_lint_core(root, load_fha_yaml(root))
            self.assertEqual([f for f in findings if f.code == 'W120'], [], sex)

    def test_unrecognised_value_is_named_in_the_message(self) -> None:
        # A lowercase 'f' is not in the vocabulary: it lands in the father
        # slot AND the message must say what was found rather than claim
        # nothing was recorded.
        root = _build(self._files('f'), root_person=KID)
        index_mod.build_index(root, load_fha_yaml(root))
        res, out, _err = _run_brackets(root)
        self.assertEqual(res.data['w120'], 1)
        self.assertIn('`sex: f`', out)
        self.assertIn('do not recognise', out)
        findings, _ = lint._run_lint_core(root, load_fha_yaml(root))
        w120 = [f for f in findings if f.code == 'W120']
        self.assertEqual(len(w120), 1)
        self.assertIn('`sex: f`', w120[0].message)

    def test_views_brackets_reports_w120(self) -> None:
        root = _build(self._files('U'), root_person=KID)
        index_mod.build_index(root, load_fha_yaml(root))
        res, out, _err = _run_brackets(root)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertEqual(res.data['w120'], 1)
        self.assertIn('W120', out)
        self.assertIn('Ma Solo', out)
        self.assertIn('sex: M', out)   # the named one-line fix

    def test_recorded_sex_silences_w120(self) -> None:
        for sex, expected_pos in (('F', 3), ('M', 2)):
            root = _build(self._files(sex), root_person=KID)
            index_mod.build_index(root, load_fha_yaml(root))
            conn = _open(root)
            try:
                gaps: list[dict] = []
                pos = build_ahnentafel_map(conn, KID.lower(), gaps)
            finally:
                conn.close()
            self.assertEqual(pos.get(MA.lower()), expected_pos, sex)
            self.assertEqual(gaps, [], sex)
            res, _out, _err = _run_brackets(root)
            self.assertEqual(res.data['w120'], 0, sex)

    def test_two_resolved_unknown_parents_stay_silent(self) -> None:
        # The genuine same-sex/unknown-pair case: the deterministic tie-break
        # is the DESIRED behavior and gets no warning.
        root = _build(self._files('U', two_parents=True), root_person=KID)
        index_mod.build_index(root, load_fha_yaml(root))
        res, _out, _err = _run_brackets(root)
        self.assertEqual(res.data['w120'], 0)

    def test_lint_reports_w120_and_matches_views(self) -> None:
        root = _build(self._files('U'), root_person=KID)
        findings, _ = lint._run_lint_core(root, load_fha_yaml(root))
        w120 = [f for f in findings if f.code == 'W120']
        self.assertEqual(len(w120), 1)
        self.assertIn('Ma Solo', w120[0].message)
        self.assertIn('sex: M', w120[0].message)
        # Sex recorded -> the lint finding disappears too.
        root2 = _build(self._files('F'), root_person=KID)
        findings2, _ = lint._run_lint_core(root2, load_fha_yaml(root2))
        self.assertEqual([f for f in findings2 if f.code == 'W120'], [])

    def test_realign_with_only_w120_says_nothing_to_realign(self) -> None:
        # MA curated, correctly filed, bracket current - the only note left is
        # the W120 data gap, which no fix pass can settle.
        files = {
            f'people/002 Ma Solo [Kid]/solo__ma_{MA}.md':
                _ptext(MA, 'Ma Solo', 'U', 'curated'),
            f'people/002 Ma Solo [Kid]/solo__kid_{KID}.md':
                _ptext(KID, 'Kid Solo', 'F', 'curated'),
            f'sources/notes/{SID.lower()}.md': (
                f'---\nid: {SID}\ntitle: Rel\nsource_type: other\n---\n\n'
                f'## Claims\n```yaml\n'
                f'{_rel_claim("C-3333333333", KID, [MA])}```\n'),
        }
        root = _build(files, root_person=KID)
        index_mod.build_index(root, load_fha_yaml(root))
        res, out, _err = _run_brackets(root, realign=True)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertEqual(res.data['w120'], 1)
        self.assertIn('Nothing to realign', out)


class BrokenFixtureTests(unittest.TestCase):
    """The standing broken fixture must keep firing its targeted code
    (BUILD.md testing invariants)."""

    def test_broken_w120_fixture_fires(self) -> None:
        root = ROOT / 'tests' / 'fixtures' / 'broken-W120'
        findings, _ = lint._run_lint_core(root, load_fha_yaml(root))
        w120 = [f for f in findings if f.code == 'W120']
        self.assertEqual(len(w120), 1)
        self.assertIn('Pat Doe', w120[0].message)


if __name__ == '__main__':
    unittest.main()
