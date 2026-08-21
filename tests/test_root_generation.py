"""
test_root_generation.py - fha.yaml `root_generation: self | children`
(SPEC §12.2/§12.4, issue #72).

`root_person` names WHO the archive is centred on; `root_generation` says
which Ahnentafel slot that person occupies:
  - 'self' (default) - root_person IS #1, exactly today's behavior.
  - 'children' - root_person is #2; #1 is left to root_person's own
    children, collectively (SPEC §12.2's own model). This lets a researcher
    anchor the archive at themselves without misnaming one of their own
    children as root_person (or having no option at all if childless).

Covers: `_lib.resolve_root_generation` / `root_generation_seed_position`
directly; self-mode back-compat (implicit and explicit); children-mode's
exact shift from self-mode (every position comes out at `self_position +
2**generation_depth` - derived from the walk's own 2n / 2n+1 recursion, not
just pinned output - see `ChildrenModeShiftTests` for the derivation); an
invalid value rejected with a clear message at every call site (lint, views
brackets, person promote, report) rather than silently read as 'self'; W127
firing under self and falling silent under children for the identical
fixture (the crux of #72); and `fha views brackets --realign` correctly
renumbering and re-filing an already-promoted tree after a root_generation
change.

Both backends - lint's in-memory registry and views'/`_lib`'s SQLite-backed
walk - are exercised throughout, matching the twin-testing convention
`test_bloodline_ahnentafel.py` already established for this derivation.
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
import person
import report
import index as index_mod
from _lib import (
    EXIT_FAILURE,
    FhaConfigError,
    ahnentafel_generation,
    couple_folder_prefix,
    load_fha_yaml,
    resolve_root_generation,
    root_generation_seed_position,
)

# ── fixture people ──────────────────────────────────────────────────────────
X = 'P-xaaaaaaaaa'      # the researcher / anchor (root_person)
PA = 'P-paaaaaaaaa'     # X's father
MA = 'P-maaaaaaaaa'     # X's mother
GPA = 'P-gpaaaaaaaa'    # PA's father
GMA = 'P-gmaaaaaaaa'    # PA's mother
SID = 'S-rezaaaaaaa'


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


def _source(sid: str, claims_yaml: str) -> str:
    return (f'---\nid: {sid}\ntitle: Rel\nsource_type: other\n---\n\n'
            f'## Claims\n```yaml\n{claims_yaml}```\n')


def _fha_yaml_text(root_person: str | None, root_generation: str | None) -> str:
    cfg = ''
    if root_person:
        cfg += f'root_person: {root_person}\n'
    if root_generation is not None:
        cfg += f'root_generation: {root_generation}\n'
    cfg += 'roots:\n  documents: documents\n'
    return cfg


def _build(files: dict[str, str], root_person: str | None = None,
           root_generation: str | None = None) -> Path:
    root = Path(tempfile.mkdtemp())
    (root / 'people' / 'stubs').mkdir(parents=True)
    (root / 'sources' / 'notes').mkdir(parents=True)
    (root / 'fha.yaml').write_text(
        _fha_yaml_text(root_person, root_generation), encoding='utf-8')
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
    return root


def _set_root_generation(root: Path, root_person: str, root_generation: str | None) -> None:
    """Rewrite fha.yaml with a different root_generation - simulates the
    human editing the config after the tree has already been promoted."""
    (root / 'fha.yaml').write_text(
        _fha_yaml_text(root_person, root_generation), encoding='utf-8')


def _open(root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(root / '.cache' / 'index.sqlite'))
    conn.row_factory = sqlite3.Row
    return conn


def _reindex(root: Path) -> None:
    index_mod.build_index(root, load_fha_yaml(root))


def _three_gen_files() -> dict[str, str]:
    """X (stub) -> PA/MA (stubs) -> GPA/GMA (PA's own parents, stubs)."""
    claims = (_rel_claim('C-1111111111', X, [PA, MA])
              + _rel_claim('C-2222222222', PA, [GPA, GMA]))
    return {
        f'people/stubs/x__kid_{X}.md': _ptext(X, 'X Kid', 'F'),
        f'people/stubs/pa__line_{PA}.md': _ptext(PA, 'Pa Line', 'M'),
        f'people/stubs/ma__line_{MA}.md': _ptext(MA, 'Ma Line', 'F'),
        f'people/stubs/gpa__line_{GPA}.md': _ptext(GPA, 'Gpa Line', 'M'),
        f'people/stubs/gma__line_{GMA}.md': _ptext(GMA, 'Gma Line', 'F'),
        f'sources/notes/{SID.lower()}.md': _source(SID, claims),
    }


def _lint_positions(root: Path, root_pid: str) -> dict:
    """The lint-backend Ahnentafel map, resolving root_generation exactly as
    `lint._check_ahnentafel_placement` does."""
    _findings, reg = lint._run_lint_core(root, load_fha_yaml(root))
    genetic = lint._build_children_of(reg, genetic_only=True)
    rg = resolve_root_generation(reg.fha_config)
    return lint._build_ahnentafel_lint(
        root_pid, genetic, reg, root_position=root_generation_seed_position(rg))


def _views_positions(root: Path, root_pid: str) -> dict:
    """The views/_lib-backend Ahnentafel map, same resolution rule."""
    _reindex(root)
    conn = _open(root)
    try:
        cfg = load_fha_yaml(root)
        rg = resolve_root_generation(cfg)
        return views._build_ahnentafel_map(
            conn, root_pid, root_position=root_generation_seed_position(rg))
    finally:
        conn.close()


class ResolveRootGenerationTests(unittest.TestCase):
    """Direct unit coverage of the shared validation helpers every call site
    (`lint`, `views`, `person`, `report`) delegates to."""

    def test_absent_key_defaults_to_self(self) -> None:
        self.assertEqual(resolve_root_generation({}), 'self')

    def test_explicit_self(self) -> None:
        self.assertEqual(resolve_root_generation({'root_generation': 'self'}), 'self')

    def test_explicit_children(self) -> None:
        self.assertEqual(
            resolve_root_generation({'root_generation': 'children'}), 'children')

    def test_case_and_whitespace_tolerant(self) -> None:
        self.assertEqual(
            resolve_root_generation({'root_generation': ' Children '}), 'children')
        self.assertEqual(resolve_root_generation({'root_generation': 'SELF'}), 'self')

    def test_seed_position_mapping(self) -> None:
        self.assertEqual(root_generation_seed_position('self'), 1)
        self.assertEqual(root_generation_seed_position('children'), 2)

    def test_invalid_value_raises_naming_both_valid_values(self) -> None:
        # "rejected with a clear message, not a silent fallback" - the
        # message must be self-sufficient (it lands in a lint Finding, a
        # CLI refusal, and a report note verbatim, none of which add their
        # own explanation), so it must name the bad value AND both options.
        with self.assertRaises(FhaConfigError) as ctx:
            resolve_root_generation({'root_generation': 'child'})
        msg = str(ctx.exception)
        self.assertIn('child', msg)
        self.assertIn('self', msg)
        self.assertIn('children', msg)

    def test_empty_string_is_invalid_not_treated_as_self(self) -> None:
        # A half-finished hand-edit ("root_generation:" with nothing after
        # it) must not silently mean the default - it means "fix this line".
        with self.assertRaises(FhaConfigError):
            resolve_root_generation({'root_generation': ''})

    def test_unrelated_keys_never_trip_it(self) -> None:
        self.assertEqual(
            resolve_root_generation({'root_person': 'P-xxxxxxxxxx'}), 'self')


class SelfModeBackCompatTests(unittest.TestCase):
    """root_generation: self - the default, written implicitly or
    explicitly - reproduces today's numbering byte-for-byte. Both backends."""

    def test_omitted_key_matches_explicit_self_lint(self) -> None:
        implicit = _build(_three_gen_files(), root_person=X)
        explicit = _build(_three_gen_files(), root_person=X, root_generation='self')
        self.assertEqual(
            _lint_positions(implicit, X.lower()), _lint_positions(explicit, X.lower()))

    def test_omitted_key_matches_explicit_self_views(self) -> None:
        implicit = _build(_three_gen_files(), root_person=X)
        explicit = _build(_three_gen_files(), root_person=X, root_generation='self')
        self.assertEqual(
            _views_positions(implicit, X.lower()), _views_positions(explicit, X.lower()))

    def test_self_mode_positions_match_spec_both_backends(self) -> None:
        # Textbook Ahnentafel: X=1 (unnumbered proband), father=2, mother=3,
        # paternal grandparents=4/5 (SPEC §12.2).
        root = _build(_three_gen_files(), root_person=X, root_generation='self')
        expected = {
            X.lower(): 1, PA.lower(): 2, MA.lower(): 3,
            GPA.lower(): 4, GMA.lower(): 5,
        }
        self.assertEqual(_lint_positions(root, X.lower()), expected)
        root2 = _build(_three_gen_files(), root_person=X, root_generation='self')
        self.assertEqual(_views_positions(root2, X.lower()), expected)


class ChildrenModeShiftTests(unittest.TestCase):
    """root_generation: children anchors root_person at #2 instead of #1.

    Ahnentafel position N, written in binary, is a leading '1' (the root)
    followed by one bit per generation (0 = father's slot, 1 = mother's) on
    the path down to that person - so N = 2**depth + path, where `path` is
    that bit-suffix as an integer and `depth` is the generation count
    (`_lib.ahnentafel_generation`). Re-seeding the SAME walk at #2 instead of
    #1 changes only the leading bit from '1' to '10' - the per-generation
    path bits are untouched - so the children-mode position is always
    `2**depth + 2**depth + path = self_mode_position + 2**depth`. That is
    the spec-grounded "known-good expected numbering" this shift is checked
    against (derived from the walk's own father/mother recursion, not a
    pinned snapshot of whatever the code currently happens to output).
    Concretely: root_person itself (depth 0) goes from 1 to 1+1=2; its
    parents (depth 1) go from 2/3 to 2+2=4 / 3+2=5; grandparents (depth 2)
    would go from 4-7 to 4+4..7+4 = 8-11; and so on.
    """

    def _assert_children_mode_matches_self_mode(self, pos_self: dict, pos_children: dict) -> None:
        self.assertTrue(pos_self)  # sanity: the fixture actually derived something
        for pid, pos in pos_self.items():
            expected = pos + 2 ** ahnentafel_generation(pos)
            self.assertEqual(pos_children[pid], expected, pid)

    def test_children_mode_shift_matches_the_walk_recursion_lint(self) -> None:
        # Anchored at PA under both modes: self treats PA as the proband
        # (an unusual but legal state - nothing requires the anchor to be
        # childless under self); children leaves PA's own child X as the
        # unpopulated #1 and puts PA at #2 instead.
        self_root = _build(_three_gen_files(), root_person=PA, root_generation='self')
        children_root = _build(_three_gen_files(), root_person=PA, root_generation='children')
        pos_self = _lint_positions(self_root, PA.lower())
        pos_children = _lint_positions(children_root, PA.lower())
        self._assert_children_mode_matches_self_mode(pos_self, pos_children)

    def test_children_mode_shift_matches_the_walk_recursion_views(self) -> None:
        self_root = _build(_three_gen_files(), root_person=PA, root_generation='self')
        children_root = _build(_three_gen_files(), root_person=PA, root_generation='children')
        pos_self = _views_positions(self_root, PA.lower())
        pos_children = _views_positions(children_root, PA.lower())
        self._assert_children_mode_matches_self_mode(pos_self, pos_children)

    def test_children_mode_concrete_numbers_both_backends(self) -> None:
        # root_person=PA, root_generation: children -> PA is #2 (PA's child
        # X would be #1, collectively, but X is never seeded or visited -
        # #1 has no database row). PA's own parents GPA/GMA land at #4/#5 -
        # one generation further out than self mode's #2/#3.
        root = _build(_three_gen_files(), root_person=PA, root_generation='children')
        expected = {PA.lower(): 2, GPA.lower(): 4, GMA.lower(): 5}
        lint_pos = _lint_positions(root, PA.lower())
        self.assertEqual(lint_pos, expected)
        self.assertNotIn(X.lower(), lint_pos)

        root2 = _build(_three_gen_files(), root_person=PA, root_generation='children')
        views_pos = _views_positions(root2, PA.lower())
        self.assertEqual(views_pos, expected)
        self.assertNotIn(X.lower(), views_pos)

    def test_root_person_itself_is_now_placeable_at_2(self) -> None:
        # Under self mode position 1 is excluded from folder placement
        # (couple_folder_prefix/W110 skip pos < 2); under children mode
        # root_person derives position 2 and IS placeable - the owner's own
        # words from the issue: "I am the root at 002."
        root = _build(_three_gen_files(), root_person=PA, root_generation='children')
        pos = _views_positions(root, PA.lower())
        self.assertEqual(pos[PA.lower()], 2)
        self.assertEqual(couple_folder_prefix(pos[PA.lower()]), 2)

    def test_childless_researcher_still_gets_a_position(self) -> None:
        # #72's other motivating case: a researcher with NO children on
        # record at all still derives a real position under root_generation:
        # children (unlike naming a child as root_person, which has no
        # answer here). GPA has no children entered in this fixture, only
        # its own parent GPA is looked up (none exist), so the map is just
        # {GPA: 2} - still correct, still placeable.
        files = {
            f'people/stubs/gpa__line_{GPA}.md': _ptext(GPA, 'Gpa Line', 'M'),
        }
        root = _build(files, root_person=GPA, root_generation='children')
        pos = _views_positions(root, GPA.lower())
        self.assertEqual(pos, {GPA.lower(): 2})


class InvalidRootGenerationTests(unittest.TestCase):
    """An invalid root_generation is rejected with a clear message - never
    silently read as 'self' - at every call site (#72)."""

    def test_lint_emits_w129_and_skips_the_whole_ahnentafel_pass(self) -> None:
        root = _build(_three_gen_files(), root_person=X, root_generation='sometime')
        findings, _reg = lint._run_lint_core(root, load_fha_yaml(root))
        w129 = [f for f in findings if f.code == 'W129']
        self.assertEqual(len(w129), 1, findings)
        self.assertIn('sometime', w129[0].message)
        self.assertIn('self', w129[0].message)
        self.assertIn('children', w129[0].message)
        # Nothing guessed: the whole derivation-dependent set stays silent
        # rather than assuming 'self' and reporting possibly-wrong numbers.
        for code in ('W110', 'W119', 'W127'):
            self.assertFalse([f for f in findings if f.code == code], code)

    def test_views_brackets_warns_and_skips_ahnentafel_checks(self) -> None:
        root = _build(_three_gen_files(), root_person=X, root_generation='sometime')
        _reindex(root)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            res = views.run_brackets(root)
        self.assertIn('sometime', err.getvalue())
        self.assertIn('self', err.getvalue())
        self.assertIn('children', err.getvalue())
        self.assertEqual(res.data.get('w110', 0), 0)
        self.assertEqual(res.data.get('w119', 0), 0)
        self.assertEqual(res.data.get('w127', 0), 0)

    def test_person_promote_refuses_outright_nothing_written(self) -> None:
        # A mutating verb must not guess 'self' and file someone into a
        # possibly-wrong couple folder from a silently-wrong assumption.
        root = _build(_three_gen_files(), root_person=X, root_generation='sometime')
        _reindex(root)
        stub_path = root / 'people' / 'stubs' / f'pa__line_{PA}.md'
        before = stub_path.read_bytes()
        res = person.run_promote(root, PA)
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertEqual(res.data['status'], 'refused')
        all_text = ' '.join(m.text for m in res.messages)
        self.assertIn('self', all_text)
        self.assertIn('children', all_text)
        # Byte-identical: truly nothing written.
        self.assertTrue(stub_path.exists())
        self.assertEqual(stub_path.read_bytes(), before)

    def test_report_promotion_candidates_degrades_with_a_plain_note(self) -> None:
        # fha report must never crash over one bad config line - it
        # degrades the same way the promotion.claims_threshold fallback
        # does elsewhere in the same function: skip the affected bucket,
        # say why in plain words, keep going.
        root = _build(_three_gen_files(), root_person=X, root_generation='sometime')
        _reindex(root)
        conn = _open(root)
        try:
            lines = report._section_promotion_candidates(conn, load_fha_yaml(root))
        finally:
            conn.close()
        joined = ' '.join(lines)
        self.assertIn('self', joined)
        self.assertIn('children', joined)


class W127RootGenerationInteractionTests(unittest.TestCase):
    """The crux of #72: a KNOWING root_generation: children must not trip
    W127, which exists to catch the ACCIDENTAL version of the identical
    setup (root_person anchored above the youngest generation with no
    explanation)."""

    def test_self_mode_still_warns_when_root_person_has_a_child(self) -> None:
        # Baseline: unchanged pre-#72 behavior. root_person=PA has a child
        # (X) on record, root_generation left at self (the default) - W127
        # fires exactly as it always has.
        root = _build(_three_gen_files(), root_person=PA)
        findings, _reg = lint._run_lint_core(root, load_fha_yaml(root))
        self.assertTrue([f for f in findings if f.code == 'W127'], findings)

        root2 = _build(_three_gen_files(), root_person=PA)
        _reindex(root2)
        conn = _open(root2)
        try:
            v = views._check_w127_root_anchor(conn, PA.lower())
        finally:
            conn.close()
        self.assertTrue(v)

    def test_children_mode_silences_w127_for_the_identical_setup_lint(self) -> None:
        # Same fixture, same root_person=PA with the identical child (X) on
        # record - only root_generation changed. W127 must be silent: SPEC
        # §12.2 says this is now the deliberate, documented shape.
        root = _build(_three_gen_files(), root_person=PA, root_generation='children')
        findings, _reg = lint._run_lint_core(root, load_fha_yaml(root))
        self.assertFalse([f for f in findings if f.code == 'W127'], findings)

    def test_children_mode_silences_w127_end_to_end_via_views_brackets(self) -> None:
        # The caller in `run_brackets` skips calling `_check_w127_root_anchor`
        # entirely under root_generation: children - end-to-end through the
        # actual command a human runs, not just the checker function alone.
        root = _build(_three_gen_files(), root_person=PA, root_generation='children')
        _reindex(root)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            res = views.run_brackets(root)
        self.assertEqual(res.data.get('w127', 0), 0)
        self.assertNotIn('W127', out.getvalue())

    def test_switching_to_children_mode_silences_a_previously_firing_w127(self) -> None:
        # The same archive, before and after the human adds the new field -
        # W127 goes from firing to silent with no other change, demonstrating
        # this is a real fix for the false alarm, not just a fresh fixture
        # that happens never to trip it.
        root = _build(_three_gen_files(), root_person=PA)
        findings_before, _reg = lint._run_lint_core(root, load_fha_yaml(root))
        self.assertTrue([f for f in findings_before if f.code == 'W127'])

        _set_root_generation(root, PA, 'children')
        findings_after, _reg2 = lint._run_lint_core(root, load_fha_yaml(root))
        self.assertFalse([f for f in findings_after if f.code == 'W127'], findings_after)


class RealignAfterRootGenerationChangeTests(unittest.TestCase):
    """`fha views brackets --realign` must correctly renumber and re-file an
    already-promoted tree after a root_generation change - the same
    recovery path the issue names for a re-anchored root_person, now
    exercised for root_generation instead of don't-assume-it-just-works."""

    FOLDER = '002 Pa Line + Ma Line'

    def _files(self) -> dict[str, str]:
        # X is an unfiled stub; PA/MA are already curated and correctly
        # filed under root_generation: self (X=1 unnumbered, PA=2/MA=3 ->
        # folder 002) - the ordinary, converged starting state.
        claims = _rel_claim('C-1111111111', X, [PA, MA])
        return {
            f'people/stubs/x__kid_{X}.md': _ptext(X, 'X Kid', 'F'),
            f'people/{self.FOLDER}/pa__line_{PA}.md': _ptext(PA, 'Pa Line', 'M', 'curated'),
            f'people/{self.FOLDER}/ma__line_{MA}.md': _ptext(MA, 'Ma Line', 'F', 'curated'),
            f'sources/notes/{SID.lower()}.md': _source(SID, claims),
        }

    def test_self_mode_baseline_is_clean(self) -> None:
        root = _build(self._files(), root_person=X)
        _reindex(root)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            res = views.run_brackets(root)
        self.assertEqual(res.data.get('w110', 0), 0, out.getvalue())
        self.assertEqual(res.data.get('w127', 0), 0, out.getvalue())

    def test_realign_renumbers_and_refiles_after_switching_to_children(self) -> None:
        root = _build(self._files(), root_person=X)
        _reindex(root)

        # The human edits fha.yaml to anchor at themselves (X) instead of
        # naming a child - X now derives #2, PA/MA derive #4/#5.
        _set_root_generation(root, X, 'children')
        _reindex(root)  # a precondition: --realign refuses on a stale index

        # Confirm the shift actually produced findings before touching
        # anything - the guard-test discipline (this must fail pre-realign).
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            preview = views.run_brackets(root)
        self.assertGreater(preview.data.get('w110', 0), 0, out.getvalue())
        self.assertGreater(preview.data.get('w119', 0), 0, out.getvalue())
        self.assertEqual(preview.data.get('w127', 0), 0, out.getvalue())

        out2, err2 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
            applied = views.run_brackets(root, realign=True, yes=True)
        self.assertIn(applied.exit_code, (0, 1), out2.getvalue() + err2.getvalue())

        # PA/MA's folder renamed from prefix 002 to prefix 004 (their new
        # derived position).
        old_folder = root / 'people' / self.FOLDER
        self.assertFalse(old_folder.exists(), list((root / 'people').iterdir()))
        new_couple_dirs = [
            d for d in (root / 'people').iterdir()
            if d.is_dir() and d.name.startswith('004')
        ]
        self.assertEqual(len(new_couple_dirs), 1, list((root / 'people').iterdir()))
        moved_names = {p.name for p in new_couple_dirs[0].glob('*.md')}
        self.assertTrue(any(PA in n for n in moved_names), moved_names)
        self.assertTrue(any(MA in n for n in moved_names), moved_names)

        # X (a stub) was promoted into a freshly created folder at prefix
        # 002 - the owner's own "I am the root at 002."
        new_x_dirs = [
            d for d in (root / 'people').iterdir()
            if d.is_dir() and d.name.startswith('002')
        ]
        self.assertEqual(len(new_x_dirs), 1, list((root / 'people').iterdir()))
        x_names = {p.name for p in new_x_dirs[0].glob('*.md')}
        self.assertTrue(any(X in n for n in x_names), x_names)
        # The promoted PROFILE keeps its original basename (promotion moves,
        # never renames) - matched exactly, not by an `X in name` substring
        # search, because promotion also scaffolds a `..._research_{X}.md`
        # companion into the same folder, whose name contains the identical
        # P-id substring and whose glob() iteration order is not guaranteed.
        moved_x = new_x_dirs[0] / f'x__kid_{X}.md'
        self.assertTrue(moved_x.exists(), x_names)
        from _lib import read_record
        rec = read_record(moved_x)
        self.assertEqual(str(rec['meta'].get('tier')), 'curated')

        # And the tree now matches its own derivation: report-only run
        # afterward finds no more W110/W119 to fix (fully converged).
        _reindex(root)
        out3, err3 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out3), contextlib.redirect_stderr(err3):
            after = views.run_brackets(root)
        self.assertEqual(after.data.get('w110', 0), 0, out3.getvalue())
        self.assertEqual(after.data.get('w119', 0), 0, out3.getvalue())


if __name__ == '__main__':
    unittest.main()
