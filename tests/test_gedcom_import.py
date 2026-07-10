"""Tests for `fha gedcom import` (plan 06 - the Ancestry on-ramp, TOOLING §13a2).

Builds a throwaway archive, imports the crafted fixtures under
tests/fixtures/gedcom/, and asserts the whole contract: dry-run writes nothing
(byte-for-byte), apply produces spec-conformant stubs/record/claims that lint
with no errors, the living heuristic lands its documented defaults, the DATE
table translates every documented row, the re-run sentinel and rollback leave
the tree untouched, and encoding/self-import guards refuse with a plain fix.

Run: python -m pytest tests/test_gedcom_import.py -q   (from the repo root)
"""

import contextlib
import hashlib
import io
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import gedcom_import
from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    is_valid_edtf,
    load_fha_yaml,
    read_record,
)

FIXTURES = ROOT / 'tests' / 'fixtures' / 'gedcom'
SMALL = FIXTURES / 'small.ged'


def _tree_digest(root: Path) -> str:
    """One hash over every file's path + bytes - the writes-nothing oracle."""
    h = hashlib.sha256()
    for p in sorted(root.rglob('*')):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode('utf-8'))
            h.update(p.read_bytes())
    return h.hexdigest()


class _ArchiveCase(unittest.TestCase):
    """Shared throwaway-archive scaffolding (fixtures, never a real archive)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive = Path(self._tmp.name) / 'archive'
        self.archive.mkdir()
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        self.config = load_fha_yaml(self.archive, strict=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, ged: Path, **kwargs) -> tuple[object, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = gedcom_import.run_import(self.archive, self.config, ged, **kwargs)
        return result, out.getvalue(), err.getvalue()

    def _add_existing_rose(self) -> None:
        people = self.archive / 'people'
        people.mkdir(exist_ok=True)
        (people / 'hartley__rose_P-aaaaaaaaaa.md').write_text(
            '---\n'
            'id: P-aaaaaaaaaa\n'
            'aliases: [P-aaaaaaaaaa]\n'
            'name: Rose Hartley\n'
            'living: false\n'
            'birth: 1878~\n'
            'tier: stub\n'
            '---\n', encoding='utf-8')


# ── DATE translation table ─────────────────────────────────────────────────────

class DateTableTestCase(unittest.TestCase):
    def test_exact_day(self) -> None:
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('12 JAN 1850'), '1850-01-12')

    def test_month_year(self) -> None:
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('JAN 1850'), '1850-01')

    def test_bare_year(self) -> None:
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('1850'), '1850')

    def test_abt_est_cal_at_precision(self) -> None:
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('ABT 1850'), '1850~')
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('EST 1848'), '1848~')
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('CAL 1850'), '1850~')
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('ABT JAN 1850'), '1850-01~')
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('ABT 12 JAN 1850'), '1850-01-12~')

    def test_before(self) -> None:
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('BEF 1920'), '[..1920]')
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('BEF JAN 1920'), '[..1920-01]')

    def test_after_form_probe(self) -> None:
        # The plan's AFT arm is a runtime probe: emit `[X..]` only if the EDTF
        # suite validates the after-form, else omit the date. Whatever the
        # suite decides, the result must be either None or valid EDTF - the
        # importer may never write an invalid date.
        result = gedcom_import.gedcom_date_to_edtf('AFT 1850')
        if result is not None:
            self.assertTrue(is_valid_edtf(result))
        else:
            self.assertIsNone(result)
        # Today's suite has no after-form pattern, so document the live outcome.
        self.assertEqual(result, '[1850..]' if is_valid_edtf('[1850..]') else None)

    def test_between_and_from_to(self) -> None:
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('BET 1870 AND 1875'), '1870/1875')
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('FROM 1901 TO 1903'), '1901/1903')

    def test_interpreted_and_phrase_dates_omit(self) -> None:
        self.assertIsNone(gedcom_import.gedcom_date_to_edtf('INT 1902 (spring)'))
        self.assertIsNone(gedcom_import.gedcom_date_to_edtf('(deceased)'))
        self.assertIsNone(gedcom_import.gedcom_date_to_edtf('sometime nice'))
        self.assertIsNone(gedcom_import.gedcom_date_to_edtf(''))

    def test_every_emitted_form_is_valid_edtf(self) -> None:
        for raw in ('12 JAN 1850', 'JAN 1850', '1850', 'ABT 1850', 'BEF 1920',
                    'AFT 1850', 'BET 1870 AND 1875', 'FROM 1901 TO 1903'):
            edtf = gedcom_import.gedcom_date_to_edtf(raw)
            if edtf is not None:
                self.assertTrue(is_valid_edtf(edtf), f'{raw} -> {edtf} is invalid')


# ── The living: heuristic (the owner-flagged default) ─────────────────────────

class LivingHeuristicTestCase(unittest.TestCase):
    def test_death_structure_means_false_even_dateless(self) -> None:
        self.assertEqual(gedcom_import.living_flag_for_import(True, None), 'false')

    def test_old_birth_means_false(self) -> None:
        self.assertEqual(
            gedcom_import.living_flag_for_import(False, '1850', today_year=2026), 'false')

    def test_recent_birth_stays_unknown(self) -> None:
        self.assertEqual(
            gedcom_import.living_flag_for_import(False, '1990', today_year=2026), 'unknown')

    def test_no_information_stays_unknown(self) -> None:
        self.assertEqual(gedcom_import.living_flag_for_import(False, None), 'unknown')

    def test_boundary_is_strictly_more_than_110(self) -> None:
        self.assertEqual(
            gedcom_import.living_flag_for_import(False, '1916', today_year=2026), 'unknown')
        self.assertEqual(
            gedcom_import.living_flag_for_import(False, '1915', today_year=2026), 'false')

    def test_upper_bound_reading_is_conservative(self) -> None:
        # A decade uses its final year; an interval its upper bound; a
        # before-form its cutoff - `false` only when even the LATEST possible
        # birth is >110 years back (the safe error direction).
        self.assertEqual(gedcom_import._birth_year_upper('187X'), 1879)
        self.assertEqual(gedcom_import._birth_year_upper('1870/1875'), 1875)
        self.assertEqual(gedcom_import._birth_year_upper('[..1880]'), 1880)
        self.assertEqual(gedcom_import._birth_year_upper('1850~'), 1850)
        self.assertIsNone(gedcom_import._birth_year_upper(None))


# ── Dry-run ───────────────────────────────────────────────────────────────────

class DryRunTestCase(_ArchiveCase):
    def test_dry_run_writes_nothing_and_counts_right(self) -> None:
        before = _tree_digest(self.archive)
        result, out, _err = self._run(SMALL)
        self.assertEqual(_tree_digest(self.archive), before)   # byte-for-byte
        self.assertEqual(result.data['persons'], 10)
        self.assertEqual(result.data['families'], 3)
        self.assertEqual(result.data['claims'], 23)
        self.assertEqual(result.data['cited_sources'], 2)
        self.assertFalse(result.data['applied'])
        self.assertEqual(result.changed, [])
        self.assertIn('dry-run', out)
        # The dangling @I99@ CHIL and the unparseable dates make this a
        # warnings run (exit 1), never a silent clean.
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertTrue(any('@I99@' in w for w in result.data['warnings']))

    def test_plan_out_writes_full_plan_outside_archive(self) -> None:
        out_file = Path(self._tmp.name) / 'plan.txt'
        result, _out, _err = self._run(SMALL, plan_out=str(out_file))
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        text = out_file.read_text(encoding='utf-8')
        for xref in ('@I1@', '@I10@'):
            self.assertIn(xref, text)                          # uncapped
        self.assertEqual(result.changed, [])                   # not an archive write

    def test_plan_out_refused_inside_archive(self) -> None:
        before = _tree_digest(self.archive)
        result, _out, err = self._run(
            SMALL, plan_out=str(self.archive / 'notes' / 'plan.txt'))
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('out/', err)
        self.assertEqual(_tree_digest(self.archive), before)

    def test_plan_out_allowed_in_archive_out_dir(self) -> None:
        result, _out, _err = self._run(
            SMALL, plan_out=str(self.archive / 'out' / 'plan.txt'))
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertTrue((self.archive / 'out' / 'plan.txt').is_file())


# ── Apply ─────────────────────────────────────────────────────────────────────

class ApplyTestCase(_ArchiveCase):
    def _apply(self):
        result, out, err = self._run(SMALL, apply=True)
        self.assertTrue(result.data['applied'])
        return result, out, err

    def test_stub_grammar_fields_and_living_counts(self) -> None:
        self._apply()
        stubs = sorted((self.archive / 'people' / 'stubs').glob('*.md'))
        self.assertEqual(len(stubs), 10)

        by_name = {}
        for p in stubs:
            rec = read_record(p)
            by_name[str(rec['meta'].get('name'))] = (p, rec['meta'])

        # Filename grammar: {surname}__{given}_{P-id}.md; no NAME -> unknown__unknown.
        rose_path, rose = by_name['Rose Hartley']
        self.assertTrue(rose_path.name.startswith('hartley__rose_P-'))
        self.assertTrue(any(p.name.startswith('unknown__unknown_P-') for p in stubs))

        # Provisional dates + heuristic: DEAT-Y person false, 1850 birth false,
        # 1990 birth unknown.
        self.assertEqual(rose['birth'], '1878-01-12')
        self.assertEqual(rose['living'], 'false')
        thomas = by_name['Thomas Hartley'][1]
        self.assertEqual(thomas['living'], 'false')            # dateless DEAT Y
        mary = by_name['Mary Ann Cole'][1]
        self.assertEqual(mary['living'], 'false')              # b. 1850-01
        frances = by_name['Frances Thorsson'][1]
        self.assertEqual(frances['living'], 'unknown')         # b. 1990
        self.assertEqual(frances['birth'], '1990')
        # AFT birth parses to no EDTF, so no provisional date and unknown.
        ada = by_name['Ada Hartley'][1]
        self.assertNotIn('birth', ada)
        self.assertEqual(ada['living'], 'unknown')
        # Extra NAME lines land in name_variants; every stub is tier: stub.
        jon = by_name['Jon Thorsson'][1]
        self.assertEqual(jon['name_variants'], ['Jón Þórsson'])
        self.assertTrue(all(meta['tier'] == 'stub' for _, meta in by_name.values()))

    def test_source_record_shape_and_claims(self) -> None:
        result, _out, _err = self._apply()
        rec_path = next((self.archive / 'sources' / 'other').glob('*_S-*.md'))
        rec = read_record(rec_path)
        self.assertEqual(rec['parse_errors'], [])
        meta = rec['meta']
        self.assertEqual(meta['source_type'], 'other')
        self.assertEqual(meta['subtype'], 'gedcom')
        self.assertEqual(meta['source_class'], 'derivative')
        self.assertNotIn('people', meta)                       # omitted by design
        self.assertEqual(meta['files'][0]['role'], 'original')
        self.assertEqual(meta['files'][0]['original_filename'], 'small.ged')
        self.assertEqual(meta['source_date'], '2026-07-01')    # HEAD DATE 1 JUL 2026

        claims = rec['claims']
        self.assertEqual(len(claims), 23)
        for c in claims:
            for field_name in ('value', 'id', 'type', 'persons', 'status', 'confidence'):
                self.assertIn(field_name, c, f'claim missing {field_name}: {c}')
            self.assertEqual(c['status'], 'suggested')
            self.assertNotIn('reviewed', c)
            if 'date' in c:
                self.assertTrue(is_valid_edtf(str(c['date'])), c['date'])
            if 'place_text' in c:
                self.assertFalse(str(c['place_text']).lower().startswith('l-'))
            if c['type'] in ('relationship', 'marriage', 'divorce'):
                self.assertIn('roles', c)

        # The one big claims block cites the GEDCOM source id everywhere.
        sid = result.data['source_id']
        self.assertTrue(rec_path.name.endswith(f'_{sid}.md'))

        by_type = {}
        for c in claims:
            by_type.setdefault(c['type'], []).append(c)
        # PEDI adopted -> subtype: adoptive on Ben's parent-child claim.
        adoptive = [c for c in by_type['relationship'] if c.get('subtype') == 'adoptive']
        self.assertEqual(len(adoptive), 1)
        self.assertIn('Ben Hartley', adoptive[0]['value'])
        # A SOUR-cited event is medium confidence and carries the lead.
        rose_birth = next(c for c in by_type['birth'] if 'Rose Hartley' in c['value'])
        self.assertEqual(rose_birth['confidence'], 'medium')
        self.assertIn('Kansas County Marriage Records', rose_birth['notes'])
        # An uncited event stays low (an online tree is hearsay-grade).
        silas_birth = next(c for c in by_type['birth'] if 'Silas' in c['value'])
        self.assertEqual(silas_birth['confidence'], 'low')
        # Unparseable date: no date field, wording preserved in the value.
        ben_birth = next(c for c in by_type['birth'] if 'Ben Hartley' in c['value'])
        self.assertNotIn('date', ben_birth)
        self.assertIn('spring after the flood', ben_birth['value'])
        # Marriage roles follow the exporter's spouse convention.
        marr = by_type['marriage'][0]
        self.assertEqual(len(marr['roles']['spouse']), 2)
        # CONC folded with no space, CONT with a newline, into the note claim.
        note = by_type['note'][0]
        self.assertIn('until 1932', note['value'])
        self.assertIn('eldest daughter', note['notes'])
        # EVEN TYPE -> event + subtype.
        even = by_type['event'][0]
        self.assertEqual(even['subtype'], 'Homestead claim')

        # The Notes section carries the cited-databases lead list, honestly framed.
        body = rec_path.read_text(encoding='utf-8')
        self.assertIn('research leads', body)
        self.assertIn('1900 United States Federal Census', body)

    def test_anchors_point_into_the_filed_copy(self) -> None:
        self._apply()
        rec_path = next((self.archive / 'sources' / 'other').glob('*_S-*.md'))
        filed = next((self.archive / 'documents' / 'gedcom').glob('*_S-*.ged'))
        # Byte-for-byte copy of the original (originals are never modified).
        self.assertEqual(filed.read_bytes(), SMALL.read_bytes())
        filed_lines = filed.read_text(encoding='utf-8').splitlines()
        for c in read_record(rec_path)['claims']:
            n = int(str(c['anchor']).split()[-1])
            line = filed_lines[n - 1]
            if c['type'] in ('marriage', 'divorce', 'relationship'):
                self.assertTrue(('MARR' in line) or ('DIV' in line) or ('FAM' in line),
                                f'{c["anchor"]} -> {line!r}')
            else:
                # An INDI event/note anchor lands on its own tag line.
                self.assertRegex(line, r'^1 ')

    def test_audit_csv_written_last_with_mapping(self) -> None:
        result, _out, _err = self._apply()
        audit = Path(result.data['audit_csv'])
        self.assertTrue(audit.is_file())
        text = audit.read_text(encoding='utf-8')
        self.assertIn('# sha256:', text)
        self.assertIn(f'# source_id: {result.data["source_id"]}', text)
        self.assertIn('@I1@', text)
        self.assertIn('@F1@', text)
        # The audit CSV is the LAST entry in changed (written after everything).
        self.assertEqual(result.changed[-1], str(audit))

    def test_post_apply_index_lint_and_review_surface(self) -> None:
        import claim as claim_mod
        import index as index_mod
        import lint as lint_mod

        result, _out, _err = self._apply()
        self.assertEqual(index_mod.build_index(self.archive, self.config).exit_code,
                         EXIT_CLEAN)
        n_errors, _n_warnings, _e018 = lint_mod.run_lint_silent(self.archive, self.config)
        self.assertEqual(n_errors, 0, 'imported records must lint with no E-codes')

        conn = sqlite3.connect(self.archive / '.cache' / 'index.sqlite')
        try:
            # Report §1 material: the source shows its suggested backlog.
            row = conn.execute(
                "SELECT COUNT(*) FROM claims WHERE status='suggested' AND source_id=?",
                (result.data['source_id'].lower(),)).fetchone()
            self.assertEqual(row[0], 23)
            # Suggested claims are never load-bearing graph edges.
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM relationships').fetchone()[0], 0)
        finally:
            conn.close()

        # Accepting ONE relationship claim through the human gate materializes
        # its edge on the next index - the review posture working as designed.
        rec_path = next((self.archive / 'sources' / 'other').glob('*_S-*.md'))
        rel = next(c for c in read_record(rec_path)['claims']
                   if c['type'] == 'relationship')
        rr = claim_mod.run_claim(self.archive, claim_id=rel['id'], status='accepted')
        self.assertEqual(rr.exit_code, EXIT_CLEAN)
        index_mod.build_index(self.archive, self.config)
        conn = sqlite3.connect(self.archive / '.cache' / 'index.sqlite')
        try:
            self.assertGreater(
                conn.execute('SELECT COUNT(*) FROM relationships').fetchone()[0], 0)
        finally:
            conn.close()

    def test_closing_output_tells_the_review_truth(self) -> None:
        _result, out, _err = self._apply()
        self.assertIn('Imported - not yet verified', out)
        self.assertIn('fha index', out)


# ── Re-run guard + dedupe ─────────────────────────────────────────────────────

class RerunGuardTestCase(_ArchiveCase):
    def test_second_apply_refused_with_date_and_sid(self) -> None:
        result, _out, _err = self._run(SMALL, apply=True)
        sid = result.data['source_id']
        before = _tree_digest(self.archive)
        result2, _out2, err2 = self._run(SMALL, apply=True)
        self.assertEqual(result2.exit_code, EXIT_ERRORS)
        self.assertIn('already imported', err2)
        self.assertIn(sid, err2)
        self.assertRegex(err2, r'\d{4}-\d{2}-\d{2}')           # names the date
        self.assertEqual(_tree_digest(self.archive), before)   # zero writes

    def test_modified_copy_plans_cleanly_and_dedupe_flags_overlap(self) -> None:
        self._run(SMALL, apply=True)
        # A NEWER export is a different file (different hash): it must plan,
        # and the dedupe report must flag the overlap with the first import.
        modified = Path(self._tmp.name) / 'newer.ged'
        modified.write_bytes(SMALL.read_bytes() + b'\n')
        result, _out, _err = self._run(modified)
        self.assertIn(result.exit_code, (EXIT_CLEAN, EXIT_WARNINGS))
        self.assertTrue(any('Rose Hartley' in d for d in result.data['duplicates']))

    def test_dedupe_reports_but_still_imports(self) -> None:
        self._add_existing_rose()
        result, out, _err = self._run(SMALL, apply=True)
        self.assertTrue(any('P-aaaaaaaaaa' in d for d in result.data['duplicates']))
        self.assertIn('merging identities is a human decision', out)
        # Rose still arrived as a NEW stub beside the existing record.
        stubs = list((self.archive / 'people' / 'stubs').glob('hartley__rose_P-*.md'))
        self.assertEqual(len(stubs), 1)
        self.assertNotIn('P-aaaaaaaaaa', stubs[0].name)


# ── Rollback ──────────────────────────────────────────────────────────────────

class RollbackTestCase(_ArchiveCase):
    def test_midway_failure_rolls_back_everything(self) -> None:
        before = _tree_digest(self.archive)
        real_write = gedcom_import._write_text
        calls = {'n': 0}

        def failing_write(path, text):
            calls['n'] += 1
            if calls['n'] == 5:          # partway through the stubs
                raise OSError('disk full (injected)')
            real_write(path, text)

        with mock.patch.object(gedcom_import, '_write_text', failing_write):
            result, _out, err = self._run(SMALL, apply=True)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('rolled back', err)
        self.assertIn('Nothing needs cleanup', err)
        self.assertEqual(_tree_digest(self.archive), before)   # byte-identical
        self.assertFalse((self.archive / '.cache' / 'gedcom_import').exists())
        # And because no sentinel landed, a re-run applies cleanly.
        result2, _out2, _err2 = self._run(SMALL, apply=True)
        self.assertTrue(result2.data['applied'])


# ── Refusals (encoding, self-import, missing/not-GEDCOM) ─────────────────────

class RefusalTestCase(_ArchiveCase):
    def _refused(self, ged: Path, needle: str) -> None:
        before = _tree_digest(self.archive)
        result, _out, err = self._run(ged, apply=True)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn(needle, err)
        self.assertIn('re-', err.lower())                      # names a next step
        self.assertEqual(_tree_digest(self.archive), before)

    def test_ansel_refused_with_fix(self) -> None:
        self._refused(FIXTURES / 'ansel.ged', 'ANSEL')

    def test_utf16_refused_with_fix(self) -> None:
        self._refused(FIXTURES / 'utf16.ged', 'UTF-16')

    def test_self_export_refused(self) -> None:
        before = _tree_digest(self.archive)
        result, _out, err = self._run(FIXTURES / 'self-export.ged', apply=True)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('one-way bridge', err)
        self.assertEqual(_tree_digest(self.archive), before)

    def test_missing_file_refused(self) -> None:
        result, _out, err = self._run(Path(self._tmp.name) / 'nope.ged')
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('does not exist', err)

    def test_not_gedcom_refused(self) -> None:
        bogus = Path(self._tmp.name) / 'notes.ged'
        bogus.write_text('just some text\nno gedcom here\n', encoding='utf-8')
        result, _out, err = self._run(bogus)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('does not look like a GEDCOM', err)

    def test_destination_collision_refused_before_any_write(self) -> None:
        # Fresh mints make an accidental repeat collision impossible, so the
        # guard is exercised directly: plan (minting real ids), hand-place a
        # file at one planned stub path, then apply - it must refuse before
        # writing ANYTHING, naming the collision.
        plan = gedcom_import.build_plan(self.archive, self.config, SMALL)
        victim = gedcom_import._planned_stub_path(plan, plan.stubs[3])
        victim.parent.mkdir(parents=True, exist_ok=True)
        victim.write_text('hand-placed\n', encoding='utf-8')
        before = _tree_digest(self.archive)
        with self.assertRaises(gedcom_import.GedcomImportError) as ctx:
            gedcom_import.apply_plan(plan)
        self.assertIn('already exists', str(ctx.exception))
        self.assertEqual(_tree_digest(self.archive), before)
        self.assertEqual(victim.read_text(encoding='utf-8'), 'hand-placed\n')


# ── CLI routing ───────────────────────────────────────────────────────────────

class CliRoutingTestCase(_ArchiveCase):
    def test_dispatcher_routes_gedcom_import(self) -> None:
        import fha
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(['gedcom', 'import', str(SMALL), '--root', str(self.archive)])
        self.assertEqual(rc, EXIT_WARNINGS)                    # the fixture's warnings
        self.assertIn('GEDCOM import plan', out.getvalue())

    def test_dispatcher_global_root_position(self) -> None:
        import fha
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(['--root', str(self.archive), 'gedcom', 'import', str(SMALL)])
        self.assertEqual(rc, EXIT_WARNINGS)
        self.assertIn('GEDCOM import plan', out.getvalue())

    def test_dispatcher_mid_position_root(self) -> None:
        # TOOLING §1's dual-position convention: `fha gedcom --root A import …`
        # must route to the importer, not to the exporter's P-id parser.
        import fha
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(['gedcom', '--root', str(self.archive), 'import', str(SMALL)])
        self.assertEqual(rc, EXIT_WARNINGS)
        self.assertIn('GEDCOM import plan', out.getvalue())

    def test_exporter_surface_untouched(self) -> None:
        # `fha gedcom <P-id>` still reaches the exporter (here: its no-index
        # failure path, proving argparse routing was not intercepted).
        import fha
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fha.main(['gedcom', 'P-aaaaaaaaaa', '--root', str(self.archive)])
        self.assertEqual(rc, EXIT_FAILURE)                     # no index built
        self.assertNotIn('GEDCOM import plan', out.getvalue())


# ── Scale smoke ───────────────────────────────────────────────────────────────

class ScaleSmokeTestCase(_ArchiveCase):
    def _write_big_gedcom(self, n: int) -> Path:
        lines = ['0 HEAD', '1 SOUR BigApp', '1 CHAR UTF-8']
        for i in range(1, n + 1):
            lines += [f'0 @I{i}@ INDI', f'1 NAME Person{i} /Bulk/',
                      '1 BIRT', '2 DATE 1900']
        lines.append('0 TRLR')
        path = Path(self._tmp.name) / 'big.ged'
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return path

    def test_thousand_person_apply_three_mint_scans_and_progress(self) -> None:
        import _lib
        big = self._write_big_gedcom(1000)
        real_scan = _lib.scan_ids_in_tree
        calls = {'n': 0}

        def counting_scan(root):
            calls['n'] += 1
            return real_scan(root)

        with mock.patch.object(_lib, 'scan_ids_in_tree', counting_scan):
            result, out, _err = self._run(big, apply=True)
        self.assertTrue(result.data['applied'])
        self.assertEqual(result.data['persons'], 1000)
        self.assertEqual(result.data['claims'], 1000)
        # Exactly three mint batches (S, P, C) - one tree scan each, never
        # one scan per record (the plan's scale contract).
        self.assertEqual(calls['n'], 3)
        # One progress line per 100 stubs, never one line per person.
        self.assertIn('wrote 100/1,000 person stubs', out)
        self.assertNotIn('Person437', out.split('Person stubs')[0])
        # The capped plan names the --plan-out escape hatch.
        self.assertIn('... and', out)
        self.assertIn('--plan-out', out)
        stubs = list((self.archive / 'people' / 'stubs').glob('*.md'))
        self.assertEqual(len(stubs), 1000)


if __name__ == '__main__':
    unittest.main()
