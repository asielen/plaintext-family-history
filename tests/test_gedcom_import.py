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
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import gedcom
import gedcom_import
from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    is_valid_edtf,
    load_fha_yaml,
    read_record,
    stub_filename,
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
        # SPEC §11 now has the after-form (#206), so the probe lands it.
        self.assertEqual(result, '[1850..]')

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
        # An approximate year's latest reading is one year on (edtf_bounds
        # widens '1850~' to 1849-1851), so its upper bound is 1851.
        self.assertEqual(gedcom_import._birth_year_upper('1850~'), 1851)
        self.assertIsNone(gedcom_import._birth_year_upper(None))
        self.assertIsNone(gedcom_import._birth_year_upper('[1850..]'))

    def test_living_flag_uses_widened_upper_bound_for_approx_birth(self) -> None:
        # 'about 1915' can be 1916, which is not more than 110 years before
        # 2026: the person must stay unknown (treated as living), not false.
        self.assertEqual(
            gedcom_import.living_flag_for_import(False, '1915~', today_year=2026), 'unknown')
        self.assertEqual(
            gedcom_import.living_flag_for_import(False, '1915?', today_year=2026), 'unknown')
        self.assertEqual(
            gedcom_import.living_flag_for_import(False, '1914~', today_year=2026), 'false')
        self.assertEqual(
            gedcom_import.living_flag_for_import(False, '1915', today_year=2026), 'false')
        self.assertEqual(
            gedcom_import.living_flag_for_import(False, '[1700..]', today_year=2026), 'unknown')


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
        self.assertTrue(rose_path.name.startswith('hartley__rose_p-'))
        self.assertTrue(any(p.name.startswith('unknown__unknown_p-') for p in stubs))

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
        # AFT birth parses to the SPEC §11 after-form (#206) and reads back as
        # that string, not a YAML list. It is open-ended ("born after 1900":
        # she could still be young), so the living heuristic stays unknown.
        ada = by_name['Ada Hartley'][1]
        self.assertEqual(ada['birth'], '[1900..]')
        self.assertEqual(ada['living'], 'unknown')
        # Extra NAME lines land in name_variants; every stub is tier: stub.
        jon = by_name['Jon Thorsson'][1]
        self.assertEqual(jon['name_variants'], ['Jón Þórsson'])
        self.assertTrue(all(meta['tier'] == 'stub' for _, meta in by_name.values()))

    def test_an_imported_stub_reads_like_any_profile(self) -> None:
        # A stub from an Ancestry download opens with the same title, purpose
        # note and sections a hand-made record has - not bare frontmatter a
        # reader has to guess what to do with.
        self._apply()
        rose = next((self.archive / 'people' / 'stubs').glob('hartley__rose_p-*.md'))
        body = read_record(rose)['body']
        self.assertTrue(body.lstrip().startswith('# Rose Hartley\n'), body[:200])
        for heading in ('## Sources', '## Biography', '## Stories', '## Research Notes',
                        '## Friends & Family'):
            self.assertIn(heading, body)
        self.assertIn("This person's record - yours to write.", body)

    def test_source_record_shape_and_claims(self) -> None:
        result, _out, _err = self._apply()
        rec_path = next((self.archive / 'sources' / 'other').glob('*_s-*.md'))
        rec = read_record(rec_path)
        self.assertEqual(rec['parse_errors'], [])
        meta = rec['meta']
        self.assertEqual(meta['source_type'], 'other')
        self.assertEqual(meta['subtype'], 'gedcom')
        self.assertEqual(meta['source_class'], 'derivative')
        self.assertNotIn('people', meta)                       # omitted by design
        self.assertEqual(meta['files'][0]['role'], 'original')
        self.assertEqual(meta['files'][0]['original_filename'], 'small.ged')
        self.assertIn(meta['files'][0]['is_primary'], (True, 'true'))
        self.assertEqual(meta['source_date'], '2026-07-01')    # HEAD DATE 1 JUL 2026
        # SPEC §14: `ai_passes:` records AI extraction passes only. This
        # import is deterministic, so it writes none (and no `## AI Passes`
        # section or `human_reviewed`); its provenance is in ## Notes.
        text = rec_path.read_text(encoding='utf-8')
        self.assertNotIn('## AI Passes', text)
        self.assertNotIn('human_reviewed', text)
        self.assertNotIn('ai_passes', meta)
        self.assertIn('Imported from small.ged with `fha gedcom import`', text)

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
        rec_path = next((self.archive / 'sources' / 'other').glob('*_s-*.md'))
        filed = next((self.archive / 'documents' / 'gedcom').glob('*_s-*.ged'))
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
        rec_path = next((self.archive / 'sources' / 'other').glob('*_s-*.md'))
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

    def test_modified_copy_plans_as_update(self) -> None:
        self._run(SMALL, apply=True)
        # A newer download is a different file (different hash). It is read as
        # the same tree: nobody is planned a second time, so there is nothing
        # for the duplicates report to flag.
        modified = Path(self._tmp.name) / 'newer.ged'
        modified.write_bytes(SMALL.read_bytes() + b'\n')
        result, _out, _err = self._run(modified)
        self.assertIn(result.exit_code, (EXIT_CLEAN, EXIT_WARNINGS))
        self.assertEqual(result.data['mode'], 'update')
        self.assertEqual(result.data['new_people'], 0)
        self.assertEqual(result.data['duplicates'], [])

    def test_dedupe_reports_but_still_imports(self) -> None:
        self._add_existing_rose()
        result, out, _err = self._run(SMALL, apply=True)
        self.assertTrue(any('P-aaaaaaaaaa' in d for d in result.data['duplicates']))
        self.assertIn('merging identities is a human decision', out)
        # Rose still arrived as a NEW stub beside the existing record.
        stubs = list((self.archive / 'people' / 'stubs').glob('hartley__rose_p-*.md'))
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


# ── Review fixes (2026-07 gedcom findings) - one regression class per fix ─────

class ReversedRangeTestCase(unittest.TestCase):
    """Finding 5: 'BET 1900 AND 1850' must not become the inverted interval
    '1900/1850' (both halves validate independently, so is_valid_edtf passes
    it, queries see an empty window, and the living heuristic reads the wrong
    bound). A reversed range is malformed: no date, wording kept in the value."""

    def test_reversed_between_omits_date(self) -> None:
        self.assertIsNone(gedcom_import.gedcom_date_to_edtf('BET 1900 AND 1850'))

    def test_reversed_from_to_omits_date(self) -> None:
        self.assertIsNone(gedcom_import.gedcom_date_to_edtf('FROM 1903 TO 1901'))

    def test_reversed_mixed_precision_omits_date(self) -> None:
        self.assertIsNone(gedcom_import.gedcom_date_to_edtf('BET JUN 1900 AND 1900'))

    def test_in_order_and_equal_ranges_still_translate(self) -> None:
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('BET 1870 AND 1875'),
                         '1870/1875')
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('BET 1900 AND 1900'),
                         '1900/1900')
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('BET 1900 AND JUN 1900'),
                         '1900/1900-06')


class ReversedRangeIntegrationTestCase(_ArchiveCase):
    def test_reversed_range_counts_as_untranslated(self) -> None:
        ged = Path(self._tmp.name) / 'rev.ged'
        ged.write_text(
            '0 HEAD\n1 SOUR App\n1 CHAR UTF-8\n'
            '0 @I1@ INDI\n1 NAME Pat /Example/\n1 BIRT\n'
            '2 DATE BET 1900 AND 1850\n0 TRLR\n', encoding='utf-8')
        plan = gedcom_import.build_plan(self.archive, self.config, ged)
        birth = next(c for c in plan.claims if c.claim_type == 'birth')
        self.assertIsNone(birth.date)                          # never '1900/1850'
        self.assertIn('BET 1900 AND 1850', birth.value)        # wording preserved
        self.assertTrue(any('could not be translated' in w
                            and 'BET 1900 AND 1850' in w for w in plan.warnings))
        # No bogus 1900 upper bound feeds the living heuristic: stays unknown
        # (the safe error direction - treated as living by every export).
        self.assertEqual(plan.stubs[0].living, 'unknown')


class AdminTagTestCase(_ArchiveCase):
    """Finding 3: '1 CHAN / 2 DATE ...' (present on every INDI in most real
    exports) is record-audit metadata. It must stay unread - _count_unread's
    docstring's own example - never a suggested 'event: chan' claim."""

    def _plan_for(self, body: str):
        ged = Path(self._tmp.name) / 'admin.ged'
        ged.write_text('0 HEAD\n1 SOUR App\n1 CHAR UTF-8\n' + body + '0 TRLR\n',
                       encoding='utf-8')
        return gedcom_import.build_plan(self.archive, self.config, ged)

    def test_chan_never_mints_an_event_claim(self) -> None:
        plan = self._plan_for(
            '0 @I1@ INDI\n1 NAME Pat /Example/\n1 BIRT\n2 DATE 1900\n'
            '1 CHAN\n2 DATE 1 JAN 2020\n3 TIME 10:00:00\n')
        self.assertEqual([c.claim_type for c in plan.claims], ['birth'])
        self.assertFalse(any(c.subtype == 'chan' for c in plan.claims))
        self.assertIn('CHAN', dict(plan.unread_top))           # counted as unread
        self.assertGreaterEqual(plan.unread_lines, 3)          # CHAN + DATE + TIME

    def test_crea_never_mints_an_event_claim(self) -> None:
        plan = self._plan_for(
            '0 @I1@ INDI\n1 NAME Pat /Example/\n1 CREA\n2 DATE 2 FEB 2021\n')
        self.assertEqual(plan.claims, [])
        self.assertIn('CREA', dict(plan.unread_top))


class DuplicateBirthTestCase(_ArchiveCase):
    """#449: a tree that holds two birth facts for one person (Ancestry
    exports an alternate fact after the preferred one) must keep the dated
    birth on the new record, and so the right living guess. A later fact
    with no date, or a vaguer one, never wipes or overrides a dated one; two
    dates that disagree are both named in the plan."""

    def _plan(self, indi_body: str):
        ged = Path(self._tmp.name) / 'twobirths.ged'
        ged.write_text('0 HEAD\n1 SOUR App\n1 CHAR UTF-8\n'
                       '0 @I1@ INDI\n1 NAME Rose /Hartley/\n1 SEX F\n'
                       + indi_body + '0 TRLR\n', encoding='utf-8')
        return gedcom_import.build_plan(self.archive, self.config, ged)

    @staticmethod
    def _text(plan) -> str:
        return '\n'.join(gedcom_import._plan_lines(plan, applied=False, capped=False))

    def test_second_undated_birth_keeps_the_first_dated_one(self) -> None:
        plan = self._plan('1 BIRT\n2 DATE 12 JAN 1878\n2 PLAC Fairview, Kansas\n'
                          '1 BIRT\n2 PLAC Kansas\n')
        (stub,) = plan.stubs
        self.assertEqual(stub.birth, '1878-01-12')
        self.assertEqual(stub.living, 'false')
        births = [c for c in plan.claims if c.claim_type == 'birth']
        self.assertEqual(len(births), 2)                 # both facts still filed

    def test_first_undated_birth_then_a_dated_one(self) -> None:
        plan = self._plan('1 BIRT\n2 PLAC Kansas\n'
                          '1 BIRT\n2 DATE 12 JAN 1878\n2 PLAC Fairview, Kansas\n')
        (stub,) = plan.stubs
        self.assertEqual(stub.birth, '1878-01-12')
        self.assertEqual(stub.living, 'false')

    def test_a_vaguer_later_birth_does_not_override_a_precise_one(self) -> None:
        plan = self._plan('1 BIRT\n2 DATE 12 JAN 1878\n1 BIRT\n2 DATE 1878\n')
        self.assertEqual(plan.stubs[0].birth, '1878-01-12')
        # 1878 and 12 Jan 1878 agree, so the plan raises no disagreement.
        self.assertNotIn('disagree', self._text(plan))

    def test_a_more_precise_later_birth_is_preferred(self) -> None:
        plan = self._plan('1 BIRT\n2 DATE ABT 1878\n1 BIRT\n2 DATE 12 JAN 1878\n')
        self.assertEqual(plan.stubs[0].birth, '1878-01-12')

    def test_two_dated_births_that_disagree_are_both_in_the_plan(self) -> None:
        plan = self._plan('1 BIRT\n2 DATE 12 JAN 1878\n2 PLAC Fairview, Kansas\n'
                          '1 BIRT\n2 DATE ABT 1990\n')
        (stub,) = plan.stubs
        self.assertEqual(stub.birth, '1878-01-12')       # the most exact date
        # One of the tree's own dates says she may be young, so she stays
        # `unknown` (treated as living) until he settles which is right.
        self.assertEqual(stub.living, 'unknown')
        text = self._text(plan)
        self.assertIn('@I1@ Rose Hartley', text)
        self.assertIn('1878-01-12', text)
        self.assertIn('1990~', text)
        self.assertIn('disagree', text)
        self.assertIn('keeps living: unknown', text)     # says why she is hidden
        # The on-screen plan says it too, not only --plan-out.
        screen = '\n'.join(gedcom_import._plan_lines(plan, applied=False))
        self.assertIn('1990~', screen)

    def test_full_plan_lists_both_birth_and_death_disagreements(self) -> None:
        # One person can disagree twice; --plan-out must not drop the second.
        plan = self._plan('1 BIRT\n2 DATE 1878\n1 BIRT\n2 DATE 1880\n'
                          '1 DEAT\n2 DATE 1950\n1 DEAT\n2 DATE 1960\n')
        self.assertEqual(len(plan.date_conflicts), 2)
        text = self._text(plan)
        self.assertIn('1960', text)
        self.assertNotIn('more (use --plan-out', text)

    def test_two_equally_precise_births_that_disagree_keep_the_first(self) -> None:
        plan = self._plan('1 BIRT\n2 DATE 1878\n1 BIRT\n2 DATE 1880\n')
        self.assertEqual(plan.stubs[0].birth, '1878')
        self.assertIn('disagree', self._text(plan))

    def test_birth_with_only_a_place(self) -> None:
        plan = self._plan('1 BIRT\n2 PLAC Fairview, Kansas\n')
        (stub,) = plan.stubs
        self.assertIsNone(stub.birth)
        self.assertEqual(stub.living, 'unknown')
        (birth,) = plan.claims
        self.assertEqual(birth.place_text, 'Fairview, Kansas')
        self.assertIsNone(birth.date)

    def test_second_undated_death_keeps_the_dated_one(self) -> None:
        plan = self._plan('1 BIRT\n2 DATE 1878\n'
                          '1 DEAT\n2 DATE 1950\n1 DEAT\n2 PLAC Kansas\n')
        (stub,) = plan.stubs
        self.assertEqual(stub.death, '1950')
        self.assertEqual(stub.living, 'false')

    def test_an_unreadable_second_birth_date_keeps_living_unknown(self) -> None:
        # A typed 05/06/1995 could be her real birth: a date nobody could read
        # is doubt, so she is not shown as dead, and the plan says why.
        for raw in ('05/06/1995', 'INT 1995 (per cousin)', '5/6/95', 'Jun 95',
                    '19950506'):
            with self.subTest(raw=raw):
                plan = self._plan('1 BIRT\n2 DATE 12 JAN 1878\n'
                                  f'1 BIRT\n2 DATE {raw}\n')
                (stub,) = plan.stubs
                self.assertEqual(stub.birth, '1878-01-12')
                self.assertEqual(stub.living, 'unknown')
                text = self._text(plan)
                self.assertIn('keeps living: unknown', text)
                self.assertIn('line 9', text)

    def test_an_unreadable_old_or_yearless_birth_does_not_hide_him(self) -> None:
        # A colonial ancestor's dual-dated or Julian alternate (or a plain
        # "Unknown") cannot make him young, so he is not hidden as living.
        for raw in ('12 FEB 1750/51', '@#DJULIAN@ 1 FEB 1750',
                    'INT 1750 (family bible)', 'Unknown'):
            with self.subTest(raw=raw):
                plan = self._plan(f'1 BIRT\n2 DATE {raw}\n'
                                  '1 BIRT\n2 DATE 12 FEB 1751\n')
                (stub,) = plan.stubs
                self.assertEqual(stub.birth, '1751-02-12')
                self.assertEqual(stub.living, 'false')
                self.assertNotIn('keeps living: unknown', self._text(plan))

    def test_a_hebrew_calendar_alternate_birth_is_read_as_its_year(self) -> None:
        # 5600 is 1840; 5755 is 1995 and may be her real, recent birth.
        for raw, living in (('@#DHEBREW@ 5600', 'false'),
                            ('@#DHEBREW@ 5755', 'unknown')):
            with self.subTest(raw=raw):
                plan = self._plan(f'1 BIRT\n2 DATE {raw}\n1 BIRT\n2 DATE 1840\n')
                self.assertEqual(plan.stubs[0].living, living)

    def test_an_open_ended_birth_that_agrees_does_not_hide_her(self) -> None:
        # AFT 1850 and BEF 1990 both fit 12 Jan 1878: nothing disagrees, so
        # she is not hidden as possibly living.
        for alt in ('AFT 1850', 'BEF 1990'):
            for order in ('first', 'last'):
                with self.subTest(alt=alt, order=order):
                    exact, other = '1 BIRT\n2 DATE 12 JAN 1878\n', f'1 BIRT\n2 DATE {alt}\n'
                    body = exact + other if order == 'last' else other + exact
                    (stub,) = self._plan(body).stubs
                    self.assertEqual(stub.birth, '1878-01-12')
                    self.assertEqual(stub.living, 'false')

    def test_a_far_fetched_date_does_not_stop_the_import(self) -> None:
        # One typo-like year on one person must not crash the whole import.
        for raw in ('ABT 9999', 'ABT 001', 'ABT JAN 0001'):
            with self.subTest(raw=raw):
                plan = self._plan(f'1 BIRT\n2 DATE 12 JAN 1878\n1 BIRT\n2 DATE {raw}\n'
                                  f'1 DEAT\n2 DATE {raw}\n')
                self.assertEqual(plan.stubs[0].birth, '1878-01-12')

    def test_an_about_day_does_not_beat_an_exact_day(self) -> None:
        plan = self._plan('1 BIRT\n2 DATE ABT 12 JAN 1850\n1 BIRT\n2 DATE 14 JAN 1850\n')
        self.assertEqual(plan.stubs[0].birth, '1850-01-14')

    def test_single_birth_file_is_unchanged(self) -> None:
        plan = gedcom_import.build_plan(self.archive, self.config, SMALL)
        self.assertEqual(
            [(s.xref, s.birth, s.death, s.living) for s in plan.stubs],
            [('@I1@', '1878-01-12', '1950~', 'false'),
             ('@I2@', '[..1880]', None, 'false'),
             ('@I3@', '[1900..]', None, 'unknown'),
             ('@I4@', None, None, 'unknown'),
             ('@I5@', '1850-01', None, 'false'),
             ('@I6@', '1848~', None, 'false'),
             ('@I7@', '1878', None, 'false'),
             ('@I8@', '1990', None, 'unknown'),
             ('@I9@', None, None, 'unknown'),
             ('@I10@', None, None, 'unknown')])
        self.assertNotIn('disagree', self._text(plan))


class NameSubtagTestCase(_ArchiveCase):
    """`2 NSFX` end to end: the suffix reaches the stub, and the tag is not
    reported as a line this importer never read (it now reads it)."""

    def _plan_for(self, body: str):
        ged = Path(self._tmp.name) / 'nsfx.ged'
        ged.write_text('0 HEAD\n1 SOUR App\n1 CHAR UTF-8\n' + body + '0 TRLR\n',
                       encoding='utf-8')
        return gedcom_import.build_plan(self.archive, self.config, ged)

    def test_an_nsfx_only_suffix_reaches_the_stub_name_and_filename(self) -> None:
        # GUARD: the "Jr" lived only in the sub-tag, so the archive record
        # came out named "Roy Eugene Dodson" - the suffix silently gone.
        plan = self._plan_for(
            '0 @I1@ INDI\n1 NAME Roy Eugene /Dodson/\n2 NSFX Jr\n1 SEX M\n')
        stub = plan.stubs[0]
        self.assertEqual(stub.name, 'Roy Eugene Dodson Jr')
        self.assertEqual(
            gedcom_import._person_filename(stub.given, stub.surname, stub.pid),
            f'dodson__roy_eugene_jr_{stub.pid}.md')

    def test_a_read_nsfx_is_not_counted_as_a_tag_this_importer_skipped(self) -> None:
        # Honesty runs both ways: the not-read tally must not name a tag
        # whose value is now in the record.
        plan = self._plan_for(
            '0 @I1@ INDI\n1 NAME Roy Eugene /Dodson/\n2 NSFX Jr\n')
        self.assertNotIn('NSFX', dict(plan.unread_top))

    def test_the_name_slot_siblings_stay_in_the_not_read_tally(self) -> None:
        # GIVN/SURN/SPFX genuinely are not read - the split stays the
        # documented best-effort one - so they keep being reported.
        plan = self._plan_for(
            '0 @I1@ INDI\n1 NAME Jose de la Cruz\n2 GIVN Jose\n2 SURN de la Cruz\n')
        self.assertIn('SURN', dict(plan.unread_top))
        self.assertIn('GIVN', dict(plan.unread_top))


class DuplicateXrefTestCase(_ArchiveCase):
    """Finding 6: '0 @I1@ INDI' twice would map both people to ONE minted pid
    (one id burned, the second stub write truncating the first). A malformed
    GEDCOM is refused, never guessed."""

    def test_duplicate_indi_xref_refused_before_any_write(self) -> None:
        before = _tree_digest(self.archive)
        result, _out, err = self._run(FIXTURES / 'dup-xref.ged', apply=True)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('@I1@', err)                             # names the xref
        self.assertIn('more than once', err)
        self.assertIn('re-', err.lower())                      # names a next step
        self.assertEqual(_tree_digest(self.archive), before)


class ContHeadSourTestCase(_ArchiveCase):
    """Finding 2: a CONT continuation inside HEAD SOUR/NAME/DATE is legal,
    cosmetic GEDCOM - it must never leak a raw newline into the rendered
    record's 'citation: >' scalar (a column-0 continuation line there corrupts
    the whole frontmatter, and --apply used to complete anyway)."""

    def test_cont_in_head_sour_renders_a_valid_record(self) -> None:
        result, _out, _err = self._run(FIXTURES / 'cont-head.ged', apply=True)
        self.assertTrue(result.data['applied'])
        rec_path = next((self.archive / 'sources' / 'other').glob('*_s-*.md'))
        rec = read_record(rec_path)
        self.assertEqual(rec['parse_errors'], [])              # frontmatter intact
        self.assertEqual(rec['meta']['repository'], 'MegaTree Deluxe Edition')
        self.assertNotIn('\n', rec['meta']['repository'])
        self.assertIn('MegaTree Deluxe Edition', rec['meta']['citation'])

    def test_render_guard_refuses_a_corrupt_render_before_any_write(self) -> None:
        plan = gedcom_import.build_plan(self.archive, self.config, SMALL)
        plan.repository = 'Evil\nbroken: ['                    # simulate a renderer bug
        before = _tree_digest(self.archive)
        with self.assertRaises(gedcom_import.GedcomImportError) as ctx:
            gedcom_import.apply_plan(plan)
        self.assertIn('Nothing was written', str(ctx.exception))
        self.assertEqual(_tree_digest(self.archive), before)


class DeadDriveTestCase(_ArchiveCase):
    """Finding 1: a documents root on an unmounted drive must be a refusal
    (exit 3, nothing written), never an infinite ensure_parent walk - a drive
    root's .parent is itself, so the old loop never terminated."""

    def test_missing_ancestors_terminates_when_nothing_exists(self) -> None:
        # The dead-drive shape, simulated: no ancestor exists anywhere on the
        # walk. The walk must stop at the filesystem root instead of chasing
        # parent-of-root forever (a hang here IS the regression signal).
        target = Path(self._tmp.name) / 'ghost' / 'docs' / 'gedcom' / 'x.ged'
        with mock.patch.object(Path, 'exists', return_value=False):
            missing = gedcom_import._missing_ancestors(target, self.archive)
        self.assertGreater(len(missing), 0)
        self.assertLess(len(missing), 100)                     # finite

    def test_unreachable_anchor_detection(self) -> None:
        target = Path(self._tmp.name) / 'x.ged'
        self.assertIsNone(gedcom_import._unreachable_anchor(target))
        with mock.patch.object(Path, 'exists', return_value=False):
            self.assertEqual(gedcom_import._unreachable_anchor(target),
                             target.anchor)

    @unittest.skipUnless(sys.platform == 'win32',
                         'drive anchors are a Windows concept')
    def test_unreachable_anchor_real_unmounted_drive(self) -> None:
        drive = next((f'{c}:\\' for c in 'QZYXWVUTSRK'
                      if not Path(f'{c}:\\').exists()), None)
        if drive is None:
            self.skipTest('every candidate drive letter is mounted')
        dest = Path(drive) / 'family' / 'documents' / 'gedcom' / 'x.ged'
        self.assertEqual(gedcom_import._unreachable_anchor(dest), drive)
        # And the ancestor walk over the dead drive terminates on its own.
        missing = gedcom_import._missing_ancestors(dest, self.archive)
        self.assertLess(len(missing), 100)

    def test_apply_refuses_unreachable_documents_root_exit_3(self) -> None:
        before = _tree_digest(self.archive)
        with mock.patch.object(gedcom_import, '_unreachable_anchor',
                               return_value='Q:\\'):
            result, _out, err = self._run(SMALL, apply=True)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertFalse(result.ok)
        self.assertIn('roots:', err)                           # names the fix
        self.assertIn('plug', err.lower())
        self.assertEqual(result.changed, [])
        self.assertEqual(_tree_digest(self.archive), before)   # nothing written


class WorkingCopyTestCase(_ArchiveCase):
    """Finding 4: TOOLING §13d - on a WORKING_COPY machine the dry-run plan
    still runs (read-only, with the banner), but --apply is asset-mutating
    and refuses warning-level, pointing at the main archive."""

    def _mark_working_copy(self) -> None:
        (self.archive / 'WORKING_COPY').write_text(
            'this machine holds no photo/document assets\n', encoding='utf-8')

    def test_plan_runs_with_banner(self) -> None:
        self._mark_working_copy()
        before = _tree_digest(self.archive)
        result, out, err = self._run(SMALL)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)      # small.ged's warnings
        self.assertEqual(result.data['persons'], 10)
        self.assertFalse(result.data['applied'])
        self.assertIn('[working copy]', err)
        self.assertIn('GEDCOM import plan', out)
        self.assertEqual(_tree_digest(self.archive), before)

    def test_apply_refuses_warning_level(self) -> None:
        self._mark_working_copy()
        before = _tree_digest(self.archive)
        result, _out, err = self._run(SMALL, apply=True)
        self.assertTrue(result.ok)                             # a safe decline
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'working-copy')
        self.assertFalse(result.data['applied'])
        self.assertEqual(result.changed, [])
        self.assertIn('main machine', err)
        self.assertEqual(_tree_digest(self.archive), before)


class PlanOutOrderingTestCase(_ArchiveCase):
    """Finding 7 (command-audit flag 16): --plan-out is written BEFORE the
    apply, so its failure is a genuine exit-2 refusal with nothing written -
    and a post-apply refresh failure never discards changed[] or mislabels a
    completed apply as a refusal."""

    def test_plan_out_failure_refuses_before_any_write(self) -> None:
        out_file = Path(self._tmp.name) / 'plan.txt'
        before = _tree_digest(self.archive)
        with mock.patch.object(gedcom_import, '_write_plan_out',
                               side_effect=OSError('disk full (injected)')):
            result, _out, err = self._run(SMALL, apply=True, plan_out=str(out_file))
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('Nothing was imported', err)
        self.assertEqual(result.changed, [])
        self.assertEqual(_tree_digest(self.archive), before)   # exit 2 is honest
        # No sentinel landed, so the fixed-up re-run applies cleanly.
        result2, _out2, _err2 = self._run(SMALL, apply=True)
        self.assertTrue(result2.data['applied'])

    def test_plan_out_refresh_failure_after_apply_keeps_changed(self) -> None:
        out_file = Path(self._tmp.name) / 'plan.txt'
        real_write = gedcom_import._write_plan_out
        calls = {'n': 0}

        def second_write_fails(path, text):
            calls['n'] += 1
            if calls['n'] == 2:                                # the post-apply refresh
                raise OSError('yanked (injected)')
            real_write(path, text)

        with mock.patch.object(gedcom_import, '_write_plan_out', second_write_fails):
            result, _out, err = self._run(SMALL, apply=True, plan_out=str(out_file))
        self.assertTrue(result.data['applied'])
        self.assertTrue(result.ok)
        self.assertGreater(len(result.changed), 0)             # the real changed[]
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertIn('import completed', err)
        self.assertTrue(out_file.is_file())                    # pre-apply write survived

    def test_plan_out_refreshed_with_applied_header_on_success(self) -> None:
        out_file = Path(self._tmp.name) / 'plan.txt'
        result, out, _err = self._run(SMALL, apply=True, plan_out=str(out_file))
        self.assertTrue(result.data['applied'])
        text = out_file.read_text(encoding='utf-8')
        self.assertTrue(text.startswith('Applied GEDCOM import'))
        self.assertIn('Full plan written', out)


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


class ParseGedcomNameTests(unittest.TestCase):
    """A generational suffix is never the surname on the way IN either
    (issues #53/#78). The importer shares `_lib.strip_generational_suffix`
    with every other site that splits a name, so the stub it files matches
    the one `fha person new` would file for the same name."""

    def _filename(self, raw):
        _display, given, surname = gedcom_import._parse_gedcom_name(raw)
        return gedcom_import._person_filename(given, surname, 'P-0000000001')

    def test_a_slashless_name_never_files_the_suffix_as_the_surname(self):
        # GUARD: a NAME line with no surname markup - what a hand-written
        # or app-exported file routinely carries - was split on the last
        # token, so 'Roy Eugene Dodson Jr' filed as
        # `jr__roy_eugene_dodson_P-….md` and INDEXED with the surname
        # "Jr", which the next `fha gedcom` then exported back out.
        for suffix in ('Jr', 'Sr', 'II', 'III', 'IV', 'V'):
            with self.subTest(suffix=suffix):
                display, given, surname = gedcom_import._parse_gedcom_name(
                    f'Roy Eugene Dodson {suffix}')
                self.assertEqual(surname, 'Dodson')
                self.assertEqual(given, f'Roy Eugene {suffix}')
                self.assertEqual(display, f'Roy Eugene Dodson {suffix}')

    def test_a_suffix_only_name_keeps_the_surname_less_reading(self):
        # 'Roy Jr' has no more of a surname than 'Roy' alone; the suffix
        # does not promote the one remaining token into one.
        display, given, surname = gedcom_import._parse_gedcom_name('Roy Jr')
        self.assertEqual((display, given, surname), ('Roy Jr', 'Roy Jr', ''))

    def test_the_suffix_rides_in_the_given_slot_in_both_name_forms(self):
        # The point of the placement (issue #53): the son's file sorts
        # directly under the father's in a file browser.
        self.assertEqual(
            self._filename('Roy Eugene Dodson Jr'),
            'dodson__roy_eugene_jr_P-0000000001.md')
        self.assertEqual(
            self._filename('Roy Eugene /Dodson/ Jr'),
            'dodson__roy_eugene_jr_P-0000000001.md')

    def test_unsuffixed_and_surname_less_names_are_untouched(self):
        self.assertEqual(
            gedcom_import._parse_gedcom_name('Roy Eugene Dodson'),
            ('Roy Eugene Dodson', 'Roy Eugene', 'Dodson'))
        self.assertEqual(
            gedcom_import._parse_gedcom_name('Cher'), ('Cher', 'Cher', ''))
        self.assertEqual(gedcom_import._parse_gedcom_name(''), ('', '', ''))

    def test_a_surname_with_no_given_name_does_not_invent_one(self):
        # GUARD: `1 NAME /Dodson/` says this person has a surname and no
        # given name. The given slot used to fall back to the surname, so
        # the file read `dodson__dodson_P-….md` - the invented-name half
        # of the `cher__cher` defect this same change fixed in
        # `convert_mining`, and NOT what `_lib.stub_filename` answers for
        # the same name.
        self.assertEqual(
            gedcom_import._parse_gedcom_name('/Dodson/'), ('Dodson', '', 'Dodson'))
        self.assertEqual(self._filename('/Dodson/'), 'dodson__unknown_P-0000000001.md')

    # ── the `2 NSFX` sub-tag ──

    def test_a_suffix_that_lives_only_in_the_nsfx_subtag_is_not_lost(self):
        # GUARD: 5.5.1 makes NSFX a sub-tag of the name structure and a
        # great many apps put the suffix THERE and nowhere else. Reading
        # only the NAME line dropped the "Jr" out of the archive
        # altogether - the very suffix this parser exists to place.
        display, given, surname = gedcom_import._parse_gedcom_name(
            'Roy Eugene /Dodson/', 'Jr')
        self.assertEqual((display, given, surname),
                         ('Roy Eugene Dodson Jr', 'Roy Eugene Jr', 'Dodson'))
        self.assertEqual(
            gedcom_import._person_filename(given, surname, 'P-0000000001'),
            'dodson__roy_eugene_jr_P-0000000001.md')

    def test_an_nsfx_on_a_slashless_name_lands_in_the_given_slot_too(self):
        self.assertEqual(
            gedcom_import._parse_gedcom_name('Roy Eugene Dodson', 'Jr'),
            ('Roy Eugene Dodson Jr', 'Roy Eugene Jr', 'Dodson'))

    def test_an_nsfx_the_name_already_carries_is_not_doubled(self):
        # This repo's own export writes the suffix BOTH ways (trailing
        # text on `1 NAME`, and `2 NSFX` naming it structurally), so
        # re-importing an `fha gedcom` file must not yield "Dodson Jr Jr".
        # The trailing-token match ignores the period an app may add.
        for raw in ('Roy Eugene /Dodson/ Jr', 'Roy Eugene /Dodson/ Jr.',
                    'Roy Eugene Dodson Jr'):
            with self.subTest(raw=raw):
                display, given, surname = gedcom_import._parse_gedcom_name(raw, 'Jr')
                self.assertEqual(display.lower().count('jr'), 1, display)
                self.assertEqual(surname, 'Dodson')

    def test_a_non_generational_nsfx_never_reaches_the_surname_slot(self):
        # NSFX is any name suffix, not only Jr/Sr - it is folded in AFTER
        # the surname split, so "Esq" cannot become the surname.
        self.assertEqual(
            gedcom_import._parse_gedcom_name('Roy Eugene Dodson', 'Esq'),
            ('Roy Eugene Dodson Esq', 'Roy Eugene Esq', 'Dodson'))

    def test_it_files_a_person_the_way_the_shared_rule_does(self):
        # Two-sided rule, two-sided test: import and `fha person new` must
        # not drift apart again.
        # A punctuated mononym is in the list because the two answered
        # differently until `stub_slug_name`'s single-token path started
        # sanitising like every other path in it: `__obrien` here vs
        # `__o'brien` there.
        for raw in ('Roy Eugene Dodson Jr', 'Roy Eugene /Dodson/ Jr',
                    'Roy Eugene Dodson', 'Cher', 'Roy Jr', '/Dodson/',
                    '/Dodson/ Jr', "O'Brien", 'Bob/Rob', 'Ann Lot #2 Doe',
                    'Ann # Doe', 'Ann - Doe'):
            with self.subTest(raw=raw):
                display, _given, surname = gedcom_import._parse_gedcom_name(raw)
                self.assertEqual(
                    self._filename(raw),
                    stub_filename(display, 'P-0000000001', surname=surname or None))

    def test_the_only_deliberate_difference_from_the_shared_rule_is_the_cap(self):
        # `_name_slot` caps each slot at 60 characters and `stub_slug_name`
        # does not - a machine-driven import can mint thousands of files at
        # once, so it stays inside Windows path limits where a human typing
        # one name does not. Pinned here so "they agree" above is not read
        # as a promise this cap breaks silently.
        long_name = ('Bartholomew Fitzwilliam Montgomery Cholmondeley '
                     'Featherstonehaugh Wodehouse')
        display, given, surname = gedcom_import._parse_gedcom_name(long_name)
        mine = gedcom_import._person_filename(given, surname, 'P-0000000001')
        shared = stub_filename(display, 'P-0000000001', surname=surname)
        self.assertNotEqual(mine, shared)
        self.assertTrue(shared.startswith(mine[:-len('_P-0000000001.md')]), (mine, shared))
        self.assertEqual(len(mine.split('__')[1].rsplit('_P-', 1)[0]), 60)


# ── Free-form dates as Ancestry exports them (#293) ───────────────────────────

# Issue #293's table: what a person typed into Ancestry, and the reading the
# archive's own loose-date reader (`_lib.normalize_date`, the one behind
# `fha claim --date` and `fha person new --birth`) gives it. The importer
# must reach the same reading when GEDCOM's strict grammar does not fit.
_T97_TABLE = [
    ('3 December 1810', '1810-12-03'),
    ('15 Sept 1873', '1873-09-15'),
    ('after 1888', '[1888..]'),
    ('Aft. 1930', '[1930..]'),
    ('about 1910', '1910~'),
    ('Before 1951', '[..1951]'),
    ('Bef. 1900', '[..1900]'),
    ('Jul 7 1879', '1879-07-07'),
    ('1998-2002', '1998/2002'),
    ('circa 1850', '1850~'),
    ('Abt. 1891', '1891~'),
    ('3 Dec 1810', '1810-12-03'),
]

_T97_GED = (
    '0 HEAD\n1 SOUR Ancestry.com Family Trees\n1 GEDC\n2 VERS 5.5.1\n'
    '2 FORM LINEAGE-LINKED\n1 CHAR UTF-8\n'
    '0 @I1@ INDI\n1 NAME Test /Example/\n1 SEX M\n'
    '1 BIRT\n2 DATE 3 December 1810\n'
    '1 RESI\n2 DATE Jul 7 1879\n'
    '1 RESI\n2 DATE 1998-2002\n'
    '1 DEAT\n2 DATE after 1888\n'
    '1 BURI\n2 DATE about 1910\n'
    '0 @I2@ INDI\n1 NAME Other /Example/\n1 SEX F\n'
    '1 BIRT\n2 DATE 15 Sept 1873\n'
    '1 DEAT\n2 DATE Before 1951\n'
    '0 TRLR\n'
)


class LooseDateFallbackTestCase(unittest.TestCase):
    """#293: a date typed as free text falls back to the archive's loose-date
    reader instead of being left out of `date:`."""

    def test_every_issue_table_row_reads(self) -> None:
        for raw, want in _T97_TABLE:
            with self.subTest(raw=raw):
                got = gedcom_import.gedcom_date_to_edtf(raw)
                self.assertEqual(got, want)
                self.assertTrue(is_valid_edtf(got))

    def test_ambiguous_slash_date_stays_unread(self) -> None:
        # Both parts are 12 or under: day/month or month/day is a coin toss.
        self.assertIsNone(gedcom_import.gedcom_date_to_edtf('05/06/1950'))

    def test_slash_date_reads_when_one_part_is_over_12(self) -> None:
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('19/05/1987'), '1987-05-19')
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('5/19/1987'), '1987-05-19')

    def test_impossible_slash_dates_stay_unread(self) -> None:
        self.assertIsNone(gedcom_import.gedcom_date_to_edtf('19/19/1987'))
        self.assertIsNone(gedcom_import.gedcom_date_to_edtf('31/02/1950'))
        self.assertIsNone(gedcom_import.gedcom_date_to_edtf('19/00/1950'))

    def test_two_digit_year_range_is_not_read_as_a_month(self) -> None:
        # `normalize_date` passes valid EDTF through, so '1908-10' would come
        # back as October 1908. GEDCOM never writes ISO dates; typed into a
        # tree, this is the short way to write 1908 to 1910.
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('1908-10'), '1908/1910')
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('1914-18'), '1914/1918')

    def test_two_digit_suffix_that_is_not_a_later_year_stays_unread(self) -> None:
        for raw in ('1908-05', '1908-08', '1998-02'):
            with self.subTest(raw=raw):
                self.assertIsNone(gedcom_import.gedcom_date_to_edtf(raw))

    def test_hedged_two_digit_suffix_is_a_hedged_range_never_a_month(self) -> None:
        # 'about 1908-10' must never read as about October 1908. The hedge
        # goes on both endpoints, the same as 'about 1908-1910'.
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('about 1908-10'), '1908~/1910~')
        self.assertEqual(gedcom_import.gedcom_date_to_edtf('ABT 1908-10'), '1908~/1910~')
        # 'before' has no stored form on a range, and a non-later suffix is
        # not a range: both stay unread rather than becoming a month.
        for raw in ('Bef. 1908-10', 'about 1908-05', 'after 1998-02'):
            with self.subTest(raw=raw):
                self.assertIsNone(gedcom_import.gedcom_date_to_edtf(raw))

    def test_hedged_four_digit_range_and_decade_never_lose_the_hedge(self) -> None:
        # A hedge dropped off a range writes a valid but wrong date: a real
        # apply wrote 'birth: 1891/1893' for 'Abt. 1891-1893'. A hedged range
        # carries the hedge on both endpoints; a hedged decade has no stored
        # form ('188X~' is not valid), so it is left out.
        cases = [
            ('Abt. 1891-1893', '1891~/1893~'),
            ('ABT 1850-1855', '1850~/1855~'),
            ('circa 1850-1855', '1850~/1855~'),
            ('~1880-1885', '1880~/1885~'),
            ('Abt 1880 to 1885', '1880~/1885~'),
            ('Possibly 1880-1885', '1880?/1885?'),
            ('about 1908-10', '1908~/1910~'),
            ('About 1850s', None),
            ('perhaps 1880s', None),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                got = gedcom_import.gedcom_date_to_edtf(raw)
                self.assertEqual(got, want)
                if got is not None:
                    self.assertTrue(is_valid_edtf(got))

    def test_placeholder_words_never_become_dates(self) -> None:
        for raw in ('?', 'unknown', 'Deceased', 'Y', 'Private', 'living',
                    '1850..', '1850...', 'Spring 1850', 'Januar 1850',
                    'janvier 1850', '1 Jan', 'Sept 31 1873'):
            with self.subTest(raw=raw):
                self.assertIsNone(gedcom_import.gedcom_date_to_edtf(raw))

    def test_unreadable_wording_still_omits(self) -> None:
        for raw in ('sometime nice', '(deceased)', 'INT 1902 (spring)',
                    'after 1880s', '2002-1998'):
            with self.subTest(raw=raw):
                self.assertIsNone(gedcom_import.gedcom_date_to_edtf(raw))

    def test_strict_grammar_unchanged(self) -> None:
        strict = [
            ('12 JAN 1850', '1850-01-12'), ('JAN 1850', '1850-01'),
            ('1850', '1850'), ('ABT 1850', '1850~'), ('EST 1848', '1848~'),
            ('CAL 1850', '1850~'), ('ABT JAN 1850', '1850-01~'),
            ('ABT 12 JAN 1850', '1850-01-12~'), ('BEF 1920', '[..1920]'),
            ('BEF JAN 1920', '[..1920-01]'), ('AFT 1850', '[1850..]'),
            ('AFT 30 SEP 1880', '[1880-09-30..]'),
            ('BET 1870 AND 1875', '1870/1875'), ('FROM 1901 TO 1903', '1901/1903'),
            ('BET 1900 AND JUN 1900', '1900/1900-06'),
        ]
        for raw, want in strict:
            with self.subTest(raw=raw):
                self.assertEqual(gedcom_import.gedcom_date_to_edtf(raw), want)
        # A reversed range is still malformed: the fallback must not rescue it.
        for raw in ('BET 1900 AND 1850', 'FROM 1903 TO 1901', 'BET JUN 1900 AND 1900'):
            with self.subTest(raw=raw):
                self.assertIsNone(gedcom_import.gedcom_date_to_edtf(raw))


class LenientLinesTestCase(_ArchiveCase):
    """MyHeritage breaks long notes onto lines with no level number and double-
    encodes web-page codes. His note text must arrive whole and clean."""

    def _plan_text(self, body: str):
        ged = Path(self._tmp.name) / 'odd.ged'
        ged.write_text(body, encoding='utf-8')
        return gedcom_import.build_plan(self.archive, self.config, ged)

    def _mh(self):
        return gedcom_import.build_plan(
            self.archive, self.config, FIXTURES / 'myheritage-lines.ged')

    def _note(self, plan):
        return next(c for c in plan.claims if c.claim_type == 'note')

    def test_no_level_lines_join_the_note(self) -> None:
        plan = self._mh()
        note = self._note(plan)
        whole = f'{note.value} {note.notes}'
        self.assertIn('and then to her granddaughter in Fairview', whole)
        self.assertFalse(any('did not parse' in w for w in plan.warnings), plan.warnings)

    def test_join_keeps_anchor(self) -> None:
        plan = self._mh()
        self.assertEqual(self._note(plan).anchor, 'line 8')
        birth = next(c for c in plan.claims if c.claim_type == 'birth')
        self.assertEqual(birth.anchor, 'line 11')

    def test_double_encoded_br_split(self) -> None:
        plan = self._mh()
        note = self._note(plan)
        self.assertEqual(note.value, 'Rose Hartley: Rose kept the family bible')
        self.assertTrue(note.notes.startswith('It passed to her eldest daughter'))
        for c in plan.claims:
            for text in (c.value, c.notes or ''):
                for junk in ('&amp;', '&lt;', '<br'):
                    self.assertNotIn(junk, text)

    def test_citation_title_cleaned(self) -> None:
        plan = self._mh()
        birth = next(c for c in plan.claims if c.claim_type == 'birth')
        self.assertIn('GEDCOM cites: Kansas Births & Baptisms', birth.notes)

    def test_plan_text_counts_both(self) -> None:
        plan = self._mh()
        text = '\n'.join(gedcom_import._plan_lines(plan, applied=False))
        self.assertIn('2 line(s) had no level number', text)
        self.assertIn('value(s) carried web-page codes', text)

    def test_plain_ampersand_untouched(self) -> None:
        plan = gedcom_import.build_plan(
            self.archive, self.config, FIXTURES / 'plain-ampersand.ged')
        self.assertEqual(self._note(plan).value, 'Rose Hartley: Bought from Smith &amp; Sons')
        text = '\n'.join(gedcom_import._plan_lines(plan, applied=False))
        self.assertNotIn('web-page codes', text)

    def test_digit_garbage_still_malformed(self) -> None:
        plan = self._plan_text(
            '0 HEAD\n1 SOUR App\n1 CHAR UTF-8\n0 @I1@ INDI\n1 NAME Pat /Example/\n'
            '1\n100 NAME x\n0 TRLR\n')
        warn = next(w for w in plan.warnings if 'did not parse' in w)
        self.assertIn('line 6', warn)
        self.assertIn('line 7', warn)

    def test_leading_text_before_head_malformed(self) -> None:
        plan = self._plan_text(
            'stray words\n0 HEAD\n1 SOUR App\n1 CHAR UTF-8\n0 @I1@ INDI\n'
            '1 NAME Pat /Example/\n0 TRLR\n')
        warn = next(w for w in plan.warnings if 'did not parse' in w)
        self.assertIn('line 1', warn)

    _MH_HEAD = ('0 HEAD\n1 SOUR MYHERITAGE\n1 CHAR UTF-8\n0 @I1@ INDI\n'
                '1 NAME Rose /Hartley/\n')

    def test_link_in_note_kept_as_typed(self) -> None:
        # A web link pasted into a MyHeritage note must arrive working:
        # "&region=" and "&copy=" are not the "&reg"/"&copy" symbols.
        url = ('https://www.familysearch.org/search?q=Hartley&region=Kansas'
               '&copy=1&notes=yes&para=2')
        plan = self._plan_text(self._MH_HEAD + f'1 NOTE See {url}\n0 TRLR\n')
        self.assertEqual(self._note(plan).value, f'Rose Hartley: See {url}')

    def test_ampersand_words_kept(self) -> None:
        plan = self._plan_text(
            self._MH_HEAD + '1 NOTE Hartley &sect 4 of the deed; Smith&times two\n0 TRLR\n')
        self.assertEqual(self._note(plan).value,
                         'Rose Hartley: Hartley &sect 4 of the deed; Smith&times two')

    def test_complete_entities_still_decoded(self) -> None:
        plan = self._plan_text(
            self._MH_HEAD + '1 NOTE Smith &amp; Sons &copy;&nbsp;1900 &#233;t&eacute;\n0 TRLR\n')
        self.assertEqual(self._note(plan).value,
                         'Rose Hartley: Smith & Sons © 1900 été')

    def test_note_line_starting_with_a_number_joins(self) -> None:
        # "2 brothers nearby" is a broken note line, not a GEDCOM tag: its
        # words, and the words after it, must stay in the note.
        plan = self._plan_text(
            self._MH_HEAD + '1 NOTE Rose raised her family\nin Fairview with\n'
            '2 brothers nearby\nand a sister.\n1923 was a hard year\nfor all.\n'
            '1 BIRT\n2 DATE 12 JAN 1878\n0 TRLR\n')
        self.assertEqual(self._note(plan).value,
                         'Rose Hartley: Rose raised her family in Fairview with 2 brothers '
                         'nearby and a sister. 1923 was a hard year for all.')
        self.assertEqual(plan.joined_lines, 5)
        self.assertFalse(plan.warnings, plan.warnings)
        birth = next(c for c in plan.claims if c.claim_type == 'birth')
        self.assertEqual(birth.anchor, 'line 12')

    def test_note_line_starting_with_number_and_capital_joins(self) -> None:
        # "2 I remember the farm" is a broken note line too: "I" is no tag.
        plan = self._plan_text(
            self._MH_HEAD + '1 NOTE Rose told her children\nthat summer:\n'
            '2 I remember the farm\nshe said.\n0 TRLR\n')
        self.assertEqual(self._note(plan).value,
                         'Rose Hartley: Rose told her children that summer: '
                         '2 I remember the farm she said.')

    def test_lower_case_real_tags_still_read(self) -> None:
        # A hand-edited file with lower-case tags keeps its birth and its
        # CONT line break, even when the same file has a level-less line.
        plan = self._plan_text(
            '0 HEAD\n1 SOUR App\n1 CHAR UTF-8\n0 @I1@ INDI\n1 NAME Rose /Hartley/\n'
            '1 NOTE First line\nstill first\n2 cont Second line\n1 Birt\n'
            '2 DATE 12 JAN 1878\n0 TRLR\n')
        note = self._note(plan)
        self.assertEqual(note.value, 'Rose Hartley: First line still first')
        self.assertEqual(note.notes, 'Second line')
        birth = next(c for c in plan.claims if c.claim_type == 'birth')
        self.assertEqual(birth.anchor, 'line 9')

    def test_record_line_after_broken_note_keeps_its_event(self) -> None:
        # A real event line after a broken note (any tag, listed or not) is
        # a record line, never note text: the will keeps its date.
        plan = self._plan_text(
            self._MH_HEAD + '1 NOTE Kept the family bible\nin Fairview.\n'
            '1 WILL\n2 DATE 12 JAN 1910\n1 Xyzzy\n0 TRLR\n')
        self.assertEqual(self._note(plan).value,
                         'Rose Hartley: Kept the family bible in Fairview.')
        self.assertTrue(any(c.anchor == 'line 8' for c in plan.claims),
                        [(c.claim_type, c.anchor) for c in plan.claims])

    def test_lower_case_tag_untouched_without_broken_lines(self) -> None:
        # A file with no level-less lines parses exactly as before.
        plan = self._plan_text(
            '0 HEAD\n1 SOUR App\n1 CHAR UTF-8\n0 @I1@ INDI\n1 NAME Rose /Hartley/\n'
            '1 NOTE First line\n1 Birt\n2 DATE 12 JAN 1878\n0 TRLR\n')
        self.assertEqual(self._note(plan).value, 'Rose Hartley: First line')
        self.assertEqual(plan.joined_lines, 0)
        self.assertTrue(any(c.claim_type == 'birth' for c in plan.claims))

    def test_plan_line_shows_br_plainly(self) -> None:
        text = '\n'.join(gedcom_import._plan_lines(self._mh(), applied=False))
        self.assertIn('(like <br>)', text)
        self.assertNotIn('&lt;', text)

    def test_filed_copy_byte_identical(self) -> None:
        fixture = FIXTURES / 'myheritage-lines.ged'
        result, _out, _err = self._run(fixture, apply=True)
        filed = list((self.archive / 'documents' / 'gedcom').glob('*.ged'))
        self.assertEqual(len(filed), 1)
        self.assertEqual(hashlib.sha256(filed[0].read_bytes()).hexdigest(),
                         hashlib.sha256(fixture.read_bytes()).hexdigest())


class LooseDateImportTestCase(_ArchiveCase):
    """#293 end to end: t97.ged imports with every date in `date:`, and the
    plan says how many dates were read from free-form wording."""

    def _plan(self, text: str, name: str = 't97.ged'):
        ged = Path(self._tmp.name) / name
        ged.write_text(text, encoding='utf-8')
        return gedcom_import.build_plan(self.archive, self.config, ged)

    def test_t97_imports_with_no_untranslated_dates(self) -> None:
        plan = self._plan(_T97_GED)
        self.assertFalse(any('could not be translated' in w for w in plan.warnings),
                         plan.warnings)
        self.assertEqual(len(plan.claims), 7)
        self.assertTrue(all(c.date for c in plan.claims),
                        [(c.value, c.date) for c in plan.claims])
        dates = {c.value.split(' (GEDCOM')[0]: c.date for c in plan.claims}
        self.assertEqual(dates['Test Example: born 3 December 1810'], '1810-12-03')
        self.assertEqual(dates['Test Example: died after 1888'], '[1888..]')
        self.assertEqual(dates['Other Example: died Before 1951'], '[..1951]')

    def test_plan_text_counts_free_form_dates(self) -> None:
        plan = self._plan(_T97_GED)
        self.assertEqual(getattr(plan, 'loose_dates', None), 7)
        text = '\n'.join(gedcom_import._plan_lines(plan, applied=False))
        self.assertIn('7 date(s) were typed as free-form wording', text)

    def test_warning_counts_only_truly_unreadable_dates(self) -> None:
        plan = self._plan(
            '0 HEAD\n1 SOUR App\n1 CHAR UTF-8\n'
            '0 @I1@ INDI\n1 NAME Pat /Example/\n'
            '1 BIRT\n2 DATE 3 December 1810\n'
            '1 DEAT\n2 DATE sometime nice\n'
            '0 @I2@ INDI\n1 NAME Sam /Example/\n'
            '0 @F1@ FAM\n1 HUSB @I1@\n1 WIFE @I2@\n'
            '1 MARR\n2 DATE Jul 7 1835\n'
            '1 DIV\n2 DATE 05/06/1850\n'
            '0 TRLR\n', name='mixed.ged')
        marr = next(c for c in plan.claims if c.claim_type == 'marriage')
        self.assertEqual(marr.date, '1835-07-07')      # the FAM caller falls back too
        self.assertEqual(getattr(plan, 'loose_dates', None), 2)
        warning = next(w for w in plan.warnings if 'could not be translated' in w)
        self.assertTrue(warning.startswith('2 date(s) could not be translated'), warning)
        self.assertIn("'sometime nice'", warning)
        self.assertIn("'05/06/1850'", warning)
        self.assertNotIn('3 December 1810', warning)
        self.assertIn('2 other date(s)', warning)

    def test_plan_out_lists_every_free_form_read_uncapped(self) -> None:
        body = ''.join(
            f'0 @I{n}@ INDI\n1 NAME P{n} /Example/\n1 BIRT\n2 DATE {n} December 1810\n'
            for n in range(1, 6))
        plan = self._plan('0 HEAD\n1 SOUR App\n1 CHAR UTF-8\n' + body + '0 TRLR\n',
                          name='many.ged')
        self.assertEqual(plan.loose_dates, 5)
        full = '\n'.join(gedcom_import._plan_lines(plan, applied=False, capped=False))
        for n in range(1, 6):
            self.assertIn(f"@I{n}@ BIRT: '{n} December 1810'", full)
        screen = '\n'.join(gedcom_import._plan_lines(plan, applied=False))
        self.assertNotIn("@I5@ BIRT: '5 December 1810'", screen)
        self.assertIn('...', screen)

    def test_plan_out_lists_every_untranslated_date(self) -> None:
        body = ''.join(
            f'0 @I{n}@ INDI\n1 NAME P{n} /Example/\n1 BIRT\n2 DATE sometime {n}\n'
            for n in range(1, 6))
        plan = self._plan('0 HEAD\n1 SOUR App\n1 CHAR UTF-8\n' + body + '0 TRLR\n',
                          name='bad.ged')
        full = '\n'.join(gedcom_import._plan_lines(plan, applied=False, capped=False))
        for n in range(1, 6):
            self.assertIn(f"@I{n}@ BIRT: 'sometime {n}'", full)
        screen = '\n'.join(gedcom_import._plan_lines(plan, applied=False)
                           + plan.warnings)
        self.assertNotIn("'sometime 5'", screen)

    def test_dedupe_line_shows_plain_wording(self) -> None:
        people = self.archive / 'people'
        people.mkdir(exist_ok=True)
        (people / 'hartley__rose_P-aaaaaaaaaa.md').write_text(
            '---\nid: P-aaaaaaaaaa\naliases: [P-aaaaaaaaaa]\nname: Rose Hartley\n'
            'living: false\nbirth: 1849~\ntier: stub\n---\n', encoding='utf-8')
        plan = self._plan('0 HEAD\n1 SOUR App\n1 CHAR UTF-8\n'
                          '0 @I1@ INDI\n1 NAME Rose /Hartley/\n1 BIRT\n2 DATE after 1700\n'
                          '0 TRLR\n', name='plain.ged')
        self.assertEqual(len(plan.duplicates), 1, plan.duplicates)
        line = plan.duplicates[0]
        self.assertIn('b.after 1700', line)
        self.assertIn('b.about 1849', line)
        self.assertNotIn('[1700..]', line)
        self.assertNotIn('1849~', line)

    def test_head_date_uses_the_strict_grammar_only(self) -> None:
        # HEAD DATE is written by the exporting program, never typed; it feeds
        # the source record's source_date and is not a free-form read.
        plan = self._plan('0 HEAD\n1 SOUR App\n1 CHAR UTF-8\n1 DATE 15 November 2026\n'
                          '0 @I1@ INDI\n1 NAME Pat /Example/\n0 TRLR\n', name='head.ged')
        self.assertIsNone(plan.source_date)
        self.assertEqual(plan.loose_dates, 0)
        plan = self._plan('0 HEAD\n1 SOUR App\n1 CHAR UTF-8\n1 DATE 15 NOV 2026\n'
                          '0 @I1@ INDI\n1 NAME Pat /Example/\n0 TRLR\n', name='head2.ged')
        self.assertEqual(plan.source_date, '2026-11-15')

    def test_open_or_range_birth_still_matches_existing_person(self) -> None:
        # A free-form birth that is now read ('Before 1900', '1840-1860',
        # 'after 1840') must still be reported against an existing
        # 'birth: 1849~' with the same name: the windows overlap. A first-four-
        # digits comparison (1900 vs 1849) used to drop these duplicates.
        people = self.archive / 'people'
        people.mkdir(exist_ok=True)
        (people / 'hartley__rose_P-aaaaaaaaaa.md').write_text(
            '---\nid: P-aaaaaaaaaa\naliases: [P-aaaaaaaaaa]\nname: Rose Hartley\n'
            'living: false\nbirth: 1849~\ntier: stub\n---\n', encoding='utf-8')
        dated = {'@I1@': 'Before 1900', '@I2@': '1840-1860', '@I3@': 'after 1840',
                 '@I4@': '1900-1910', '@I5@': '1847'}
        body = ''.join(f'0 {x} INDI\n1 NAME Rose /Hartley/\n1 BIRT\n2 DATE {d}\n'
                       for x, d in dated.items())
        plan = self._plan('0 HEAD\n1 SOUR App\n1 CHAR UTF-8\n' + body + '0 TRLR\n',
                          name='dupes.ged')
        flagged = {line.split(' ', 1)[0] for line in plan.duplicates}
        self.assertEqual(flagged, {'@I1@', '@I2@', '@I3@', '@I5@'}, plan.duplicates)



class ExplicitSurnameRoundTripTestCase(_ArchiveCase):
    """A GEDCOM `/…/` is an explicit surname decision: the importer records
    it as `surname:` where the name alone would be read differently, so a
    re-export says what the import read (Codex, PR #285)."""

    def _import_and_export(self, body: str) -> tuple[list[str], list[str]]:
        import index
        ged = Path(self._tmp.name) / 'names.ged'
        ged.write_text('0 HEAD\n1 SOUR App\n1 CHAR UTF-8\n' + body + '0 TRLR\n',
                       encoding='utf-8')
        result, _out, err = self._run(ged, apply=True)
        self.assertTrue(result.ok, err)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            index.build_index(self.archive, self.config)
        import time
        time.sleep(0.02)
        text = gedcom.run_gedcom(self.archive, None, all_persons=True,
                                 include_living=True)['text']
        stubs = [p.read_text(encoding='utf-8')
                 for p in (self.archive / 'people').rglob('*.md')]
        return text.splitlines(), stubs

    def test_numeral_surname_survives_import_and_export(self) -> None:
        lines, stubs = self._import_and_export('0 @I1@ INDI\n1 NAME Jinping /XI/\n')
        self.assertIn('1 NAME Jinping /XI/', lines)
        self.assertTrue(any('surname: XI' in s for s in stubs))

    def test_empty_slashes_mean_no_surname(self) -> None:
        lines, stubs = self._import_and_export('0 @I1@ INDI\n1 NAME Jon Thorsson //\n')
        self.assertIn('1 NAME Jon Thorsson //', lines)
        self.assertTrue(any('surname: ""' in s for s in stubs))

    def test_surname_only_name_round_trips(self) -> None:
        lines, _stubs = self._import_and_export('0 @I1@ INDI\n1 NAME /Dodson/\n')
        self.assertIn('1 NAME /Dodson/', lines)

    def test_ordinary_surname_writes_no_line(self) -> None:
        lines, stubs = self._import_and_export('0 @I1@ INDI\n1 NAME Henry /Tudor/\n')
        self.assertIn('1 NAME Henry /Tudor/', lines)
        self.assertFalse(any('\nsurname:' in s for s in stubs))


class PersonsWrittenWithNamesTests(unittest.TestCase):
    """D6 / SPEC §8.7: imported claims name their people one per line,
    `- "[[p-…|Name]]"`; roles: stays bare IDs."""

    def test_render_claim(self) -> None:
        claim = gedcom_import._Claim(
            cid='c-1111111111', claim_type='relationship', value='parent of',
            persons=['p-aaaaaaaaaa', 'p-bbbbbbbbbb'], date=None, place_text=None,
            confidence='medium', anchor='line 4',
            roles={'child': 'p-aaaaaaaaaa', 'parent': ['p-bbbbbbbbbb']})
        lines = gedcom_import._render_claim(claim, {'p-aaaaaaaaaa': 'Ann', 'p-bbbbbbbbbb': 'Bo'})
        self.assertEqual(lines[3:6], ['  persons:', '    - "[[p-aaaaaaaaaa|Ann]]"',
                                      '    - "[[p-bbbbbbbbbb|Bo]]"'])
        self.assertIn('    child: p-aaaaaaaaaa', lines)



# ── Photos the old program linked ──────────────────────────────────────────────

import photoindex
from _lib import fha_launcher, photoindex_status, shell_quote

PHOTO_LINKS = FIXTURES / 'photo-links.ged'


class PhotoLinksTestCase(_ArchiveCase):
    """For him: the plan says which linked photos are already in his folders and
    offers each to `tag-person`; nothing is written to a photo or its keywords."""

    def setUp(self) -> None:
        super().setUp()
        self._orig_exiftool = photoindex._run_exiftool
        photoindex._run_exiftool = lambda paths: [{'SourceFile': str(p)} for p in paths]
        self.photos = self.archive / 'photos'
        for rel in ('1950/rose_wedding.jpg', '1902/thomas.jpg', '1910/thomas.jpg'):
            f = self.photos / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b'not really a jpeg: ' + rel.encode())
        self.ged_dir = Path(self._tmp.name) / 'download'
        self.ged_dir.mkdir()
        self.ged = self.ged_dir / 'photo-links.ged'
        shutil.copyfile(PHOTO_LINKS, self.ged)

    def tearDown(self) -> None:
        photoindex._run_exiftool = self._orig_exiftool
        super().tearDown()

    def _scan(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            photoindex.run_scan(self.archive, self.config)

    def _catalog(self) -> sqlite3.Connection:
        return sqlite3.connect(self.archive / '.cache' / 'photos.sqlite')

    def _record_text(self) -> str:
        return next((self.archive / 'sources' / 'other').glob('*.md')).read_text(encoding='utf-8')

    def test_found_by_name(self) -> None:
        self._scan()
        _, out, _ = self._run(self.ged)
        self.assertIn('Photos your old program linked: 9 (9 people)', out)
        self.assertRegex(out, r'Rose Hartley\s+photos/1950/rose_wedding\.jpg')

    def test_ambiguous_lists_both(self) -> None:
        self._scan()
        _, out, _ = self._run(self.ged)
        self.assertRegex(out, r'Thomas Hartley\s+thomas\.jpg could be photos/1902/thomas\.jpg '
                              r'or photos/1910/thomas\.jpg')

    def test_web_link_host_only(self) -> None:
        self._scan()
        plan_out = Path(self._tmp.name) / 'plan.txt'
        _, out, err = self._run(self.ged, apply=True, plan_out=plan_out)
        self.assertIn('ancestry.com', out)
        for text in (out, err, plan_out.read_text(encoding='utf-8'), self._record_text()):
            self.assertNotIn('token=abc', text)
            self.assertNotIn('mediaui-viewer', text)

    def test_folder_hint(self) -> None:
        self._scan()
        _, out, _ = self._run(self.ged)
        self.assertIn('"Hartley Media"', out)
        self.assertNotIn('Users', out)
        self.assertNotIn('someone', out)

    def test_catalog_absent_says_how(self) -> None:
        _, out, _ = self._run(self.ged)
        self.assertIn('Run fha photoindex', out)
        self.assertNotIn('already in your photo folders', out)

    def test_apply_lists_in_notes_and_prints_command(self) -> None:
        self._scan()
        _, dry_out, _ = self._run(self.ged)
        self.assertNotIn('fha photoindex tag-person', dry_out)
        _, out, _ = self._run(self.ged, apply=True)
        record = self._record_text()
        self.assertRegex(record, r'- \[\[p-[0-9a-z]+\|Rose Hartley\]\]: photos/1950/rose_wedding\.jpg')
        self.assertRegex(record, r'Thomas Hartley\]\]: thomas\.jpg .*photos/1902/thomas\.jpg')
        self.assertNotIn('Users', record)
        m = re.search(r'fha photoindex tag-person (\S+) --paths (.+)', out)
        self.assertIsNotNone(m, out)
        self.assertEqual(m.group(2).strip(), shell_quote('photos/1950/rose_wedding.jpg'))
        self.assertTrue(m.group(1).strip('\'"').startswith('p-'))

    def test_no_photo_or_keyword_written(self) -> None:
        self._scan()
        files = sorted(p for p in self.photos.rglob('*') if p.is_file())
        before = [(p, hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                  for p in files]
        conn = self._catalog()
        kw_before = conn.execute('SELECT COUNT(*) FROM photo_keywords').fetchone()[0]
        conn.close()
        self._run(self.ged, apply=True)
        after = [(p, hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                 for p in files]
        self.assertEqual(before, after)
        conn = self._catalog()
        self.assertEqual(kw_before, conn.execute('SELECT COUNT(*) FROM photo_keywords').fetchone()[0])
        conn.close()

    def test_obje_no_longer_unread(self) -> None:
        plan = gedcom_import.build_plan(self.archive, self.config, self.ged)
        self.assertNotIn('OBJE', dict(plan.unread_top))
        self.assertNotIn('FILE', dict(plan.unread_top))

    def test_already_tagged_counted(self) -> None:
        self._scan()
        fixed = [f'P-{c * 10}' for c in 'abcdefghi']
        conn = self._catalog()
        conn.execute('INSERT INTO photo_keywords(path, keyword) VALUES (?, ?)',
                     ('photos/1950/rose_wedding.jpg', fixed[0]))
        conn.commit()
        conn.close()

        def fake_mint(prefix, n, root):
            return fixed[:n] if prefix == 'P' else [f'{prefix}-{i:010d}' for i in range(n)]

        with mock.patch.object(gedcom_import, 'mint_ids', side_effect=fake_mint):
            plan = gedcom_import.build_plan(self.archive, self.config, self.ged)
        out = '\n'.join(gedcom_import._plan_lines(plan, applied=False))
        self.assertIn('already carry that person', out)
        self.assertNotIn('rose_wedding', out)
        self.assertEqual(
            [lk.status for lk in plan.photo_links if lk.basename == 'rose_wedding.jpg'],
            ['already tagged'])
        self.assertNotIn('rose_wedding', gedcom_import._photo_handoff_text(plan))

    def test_exact_path_and_separators(self) -> None:
        """A path written with backslashes and another letter case still finds the
        photo; a name that exists nowhere is reported not found, never guessed."""
        self._scan()
        text = self.ged.read_text(encoding='utf-8').replace(
            'C:\\Users\\someone\\Hartley Media\\rose_wedding.jpg', 'ROSE_Wedding.JPG')
        self.ged.write_text(text, encoding='utf-8')
        plan = gedcom_import.build_plan(self.archive, self.config, self.ged)
        rose = next(lk for lk in plan.photo_links if lk.person_xref == '@I1@')
        self.assertEqual((rose.status, rose.alias), ('matched', 'photos/1950/rose_wedding.jpg'))
        ben = next(lk for lk in plan.photo_links if lk.person_xref == '@I4@')
        self.assertEqual(ben.status, 'not found')

    def test_exact_path_beside_the_ged_file(self) -> None:
        self._scan()
        text = self.ged.read_text(encoding='utf-8').replace(
            'C:\\Users\\someone\\Hartley Media\\rose_wedding.jpg', '../archive/photos/1950/rose_wedding.jpg')
        self.ged.write_text(text, encoding='utf-8')
        plan = gedcom_import.build_plan(self.archive, self.config, self.ged)
        rose = next(lk for lk in plan.photo_links if lk.person_xref == '@I1@')
        self.assertEqual((rose.status, rose.how), ('matched', 'same file'))

    def test_non_image_and_file_url(self) -> None:
        self._scan()
        extra = ('0 @I10@ INDI\n1 NAME Hal /Hartley/\n'
                 '1 OBJE\n2 FILE C:\\docs\\letter.pdf\n'
                 '1 OBJE\n2 FILE file:///C:/Old%20Pics/rose_wedding.jpg\n')
        self.ged.write_text(self.ged.read_text(encoding='utf-8').replace('0 TRLR\n', extra + '0 TRLR\n'),
                            encoding='utf-8')
        plan = gedcom_import.build_plan(self.archive, self.config, self.ged)
        hal = [lk for lk in plan.photo_links if lk.person_xref == '@I10@']
        self.assertEqual([lk.basename for lk in hal], ['rose_wedding.jpg'])   # the PDF is not a photo
        self.assertEqual(hal[0].folder, 'Old Pics')

    def test_catalog_absent_keeps_the_list_in_notes(self) -> None:
        """Importing before the photo catalog exists still records which photos
        the old tree linked (a re-import is refused, so this is the only copy)."""
        _, out, _ = self._run(self.ged, apply=True)
        record = self._record_text()
        self.assertRegex(record, r'Rose Hartley\]\]: rose_wedding\.jpg .*not looked for')
        self.assertRegex(record, r'Thomas Hartley\]\]: thomas\.jpg .*not looked for')
        self.assertNotIn('Users', record)
        self.assertNotIn('fha photoindex tag-person', out)

    def test_handoff_refreshes_the_catalog_before_tagging(self) -> None:
        """The import adds people, which makes the photo catalog read stale, and
        tag-person refuses a stale catalog: the command list refreshes it first."""
        self._scan()
        _, out, _ = self._run(self.ged, apply=True)
        self.assertEqual(photoindex_status(self.archive, self.config)[0], 'stale')
        launcher = re.escape(fha_launcher())
        self.assertRegex(out, rf'\n  {launcher} photoindex\n  {launcher} photoindex tag-person ')

    def test_citation_media_is_not_a_person_photo(self) -> None:
        """A census scan attached to a citation is not a photo of the person."""
        census = self.photos / '1900' / 'census.jpg'
        census.parent.mkdir(parents=True)
        census.write_bytes(b'scan')
        self._scan()
        extra = ('0 @I10@ INDI\n1 NAME Hal /Hartley/\n'
                 '1 SOUR @S1@\n2 OBJE\n3 FILE census.jpg\n'
                 '1 RESI\n2 SOUR @S1@\n3 OBJE\n4 FILE census.jpg\n'
                 '0 @S1@ SOUR\n1 TITL 1900 census\n')
        self.ged.write_text(self.ged.read_text(encoding='utf-8').replace('0 TRLR\n', extra + '0 TRLR\n'),
                            encoding='utf-8')
        plan = gedcom_import.build_plan(self.archive, self.config, self.ged)
        self.assertEqual([lk for lk in plan.photo_links if lk.person_xref == '@I10@'], [])

    def test_obsidian_links_name_the_new_person_file(self) -> None:
        """With Obsidian links, a name in the photo list opens the person's file."""
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\nlinks: obsidian\n',
            encoding='utf-8')
        self.config = load_fha_yaml(self.archive, strict=True)
        self._scan()
        self._run(self.ged, apply=True)
        m = re.search(r'- \[\[([^|\]]+)\|Rose Hartley\]\]: photos/1950/', self._record_text())
        self.assertIsNotNone(m)
        self.assertTrue(list(self.archive.glob(f'people/**/{m.group(1)}.md')), m.group(1))

    def test_catalog_read_only_in_a_folder_with_a_hash(self) -> None:
        """An archive in a folder like "Family #2" is matched, and the read-only
        catalog open creates nothing beside it."""
        parent = Path(self._tmp.name) / 'Family #2 100%'
        parent.mkdir()
        shutil.move(str(self.archive), str(parent / 'archive'))
        self.archive = parent / 'archive'
        self.photos = self.archive / 'photos'
        self.config = load_fha_yaml(self.archive, strict=True)
        self._scan()
        before = sorted(p.name for p in Path(self._tmp.name).iterdir())
        plan = gedcom_import.build_plan(self.archive, self.config, self.ged)
        self.assertEqual(plan.photo_catalog, 'ready')
        rose = next(lk for lk in plan.photo_links if lk.person_xref == '@I1@')
        self.assertEqual(rose.status, 'matched')
        self.assertEqual(before, sorted(p.name for p in Path(self._tmp.name).iterdir()))


    def test_unreadable_catalog_notes_say_so(self) -> None:
        (self.archive / '.cache').mkdir()
        (self.archive / '.cache' / 'photos.sqlite').write_bytes(b'not a database at all')
        self._run(self.ged, apply=True)
        record = self._record_text()
        self.assertIn('rose_wedding.jpg "Wedding" (not looked for: the photo catalog could not be read', record)
        self.assertNotIn('was not built', record)

    def test_heif_and_raw_photos_in_the_catalog_are_matched(self) -> None:
        """A HEIF or camera-raw photo the catalog holds is offered like a JPEG."""
        for rel in ('1960/ada.heif', '1961/ben.dng'):
            f = self.photos / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b'photo ' + rel.encode())
        self._scan()
        text = self.ged.read_text(encoding='utf-8')
        text = text.replace('https://www.ancestry.com/mediaui-viewer/x?token=abc', 'ada.heif')
        text = text.replace('Hartley Media/missing.png', 'Hartley Media/ben.dng')
        self.ged.write_text(text, encoding='utf-8')
        plan = gedcom_import.build_plan(self.archive, self.config, self.ged)
        got = {lk.person_xref: (lk.status, lk.alias) for lk in plan.photo_links}
        self.assertEqual(got['@I3@'], ('matched', 'photos/1960/ada.heif'))
        self.assertEqual(got['@I4@'], ('matched', 'photos/1961/ben.dng'))

    def test_every_same_name_photo_is_listed(self) -> None:
        """When a name matches five photos, his choice is among all five, never
        a silent first three."""
        for year in range(1901, 1906):
            f = self.photos / str(year) / 'scan.jpg'
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b'scan ' + str(year).encode())
        self._scan()
        self.ged.write_text(self.ged.read_text(encoding='utf-8').replace(
            'Hartley Media/missing.png', 'Hartley Media/scan.jpg'), encoding='utf-8')
        _, out, _ = self._run(self.ged, apply=True)
        self.assertIn('(or 2 more, listed in the full plan', out)
        self.assertIn('photos/1905/scan.jpg', self._record_text())

    def test_document_media_is_not_offered(self) -> None:
        """A census image the old program typed as a document is not his photo."""
        doc = self.photos / 'docs' / 'rose_census_1900.jpg'
        doc.parent.mkdir(parents=True)
        doc.write_bytes(b'census')
        self._scan()
        extra = ('0 @I10@ INDI\n1 NAME Ann /Hartley/\n'
                 '1 OBJE\n2 FILE rose_census_1900.jpg\n3 FORM jpg\n4 TYPE document\n'
                 '1 OBJE @O2@\n'
                 '0 @O2@ OBJE\n1 FILE rose_census_1900.jpg\n1 _TYPE DOCUMENT\n'
                 '0 @I11@ INDI\n1 NAME Al /Hartley/\n'
                 '1 OBJE\n2 FILE rose_census_1900.jpg\n3 FORM jpg\n4 MEDI photo\n')
        self.ged.write_text(self.ged.read_text(encoding='utf-8').replace('0 TRLR\n', extra + '0 TRLR\n'),
                            encoding='utf-8')
        plan = gedcom_import.build_plan(self.archive, self.config, self.ged)
        self.assertEqual([lk for lk in plan.photo_links if lk.person_xref == '@I10@'], [])
        self.assertEqual([lk.status for lk in plan.photo_links if lk.person_xref == '@I11@'],
                         ['matched'])
        self.assertNotIn('TYPE', dict(plan.unread_top))

    def test_name_only_match_says_look_first(self) -> None:
        """The printed example warns when only the file name matched."""
        self._scan()
        _, out, _ = self._run(self.ged, apply=True)
        self.assertIn('matched by its file name only: look at it before you say yes', out)
        self.assertIn('1 is already in your photo folders:', out)

    def test_folder_hint_never_names_a_home_folder(self) -> None:
        """Photos kept straight in C:\\Users\\<name> never print that name."""
        self._scan()
        self.ged.write_text(self.ged.read_text(encoding='utf-8').replace(
            'someone\\Hartley Media\\', 'someone\\'), encoding='utf-8')
        _, out, _ = self._run(self.ged)
        self.assertNotIn('someone', out)
        self.assertNotIn('folder called', out)


class SqliteReadonlyUriTestCase(unittest.TestCase):
    """For him: an archive in a folder like "Family #2", or on a network share,
    can read its caches without a read-only look ever creating a file."""

    def test_special_characters_are_encoded(self) -> None:
        from _lib import sqlite_readonly_uri
        uri = sqlite_readonly_uri(Path('/x/Family #2 100%/.cache/photos.sqlite').absolute())
        self.assertTrue(uri.endswith('/x/Family%20%232%20100%25/.cache/photos.sqlite?mode=ro'), uri)
        self.assertTrue(uri.startswith('file:///'), uri)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows drive and share paths')
    def test_windows_drive_and_share(self) -> None:
        from _lib import sqlite_readonly_uri
        self.assertEqual(sqlite_readonly_uri(Path('D:/Family Archive/.cache/photos.sqlite')),
                         'file:///D:/Family%20Archive/.cache/photos.sqlite?mode=ro')
        share = Path(chr(92) * 2 + 'nas' + chr(92) + 'family' + chr(92) + 'photos.sqlite')
        self.assertEqual(sqlite_readonly_uri(share), 'file:////nas/family/photos.sqlite?mode=ro')

# ── Odd files from other programs ─────────────────────────────────────────────

ADVERSARIAL = FIXTURES / 'adversarial'

_CRLF_SOURCE = (
    '0 HEAD\n1 SOUR Test\n1 CHAR UTF-8\n'
    '0 @I1@ INDI\n1 NAME Rose /Hartley/\n1 SEX F\n'
    '1 BIRT\n2 DATE 12 JAN 1878\n2 PLAC Fairview, Kansas\n'
    '1 NOTE Kept the family bible\n2 CONT in a cedar box\n'
    '0 TRLR\n')


class AdversarialFixtureTestCase(_ArchiveCase):
    """Files from other programs arrive without surprises. The four files in
    adversarial/ are copied from another project (MIT; see its README); the
    expected answers below come from TOOLING §13a2 and the importer's rules,
    not from whatever the code printed. The rest are built from bytes here so
    no editor or git newline setting can change them."""

    def _plan(self, ged: Path):
        return gedcom_import.build_plan(self.archive, self.config, ged)

    def _bytes(self, name: str, raw: bytes) -> Path:
        p = Path(self._tmp.name) / name
        p.write_bytes(raw)
        return p

    def _applied_lints_clean(self, ged: Path):
        import lint as lint_mod
        result, _out, err = self._run(ged, apply=True)
        self.assertTrue(result.data['applied'], err)
        n_errors, _w, _e = lint_mod.run_lint_silent(self.archive, self.config)
        self.assertEqual(n_errors, 0, 'imported records must lint with no E-codes')
        return result

    @staticmethod
    def _of(plan, claim_type: str):
        return [c for c in plan.claims if c.claim_type == claim_type]

    # -- the four copied files ------------------------------------------------

    def test_minimal_551_one_person_one_note(self) -> None:
        plan = self._plan(ADVERSARIAL / 'minimal-551.ged')
        self.assertEqual(len(plan.stubs), 1)
        self.assertEqual(plan.stubs[0].name, 'Briar Fiction')
        self.assertEqual(plan.stubs[0].living, 'unknown')
        self.assertEqual([c.claim_type for c in plan.claims], ['note'])
        self.assertEqual(plan.warnings, [])
        result = self._applied_lints_clean(ADVERSARIAL / 'minimal-551.ged')
        self.assertEqual(result.exit_code, EXIT_CLEAN)

    def test_minimal_555_leading_zero_date_and_place(self) -> None:
        plan = self._plan(ADVERSARIAL / 'minimal-555.ged')
        (birth,) = plan.claims
        self.assertEqual(birth.claim_type, 'birth')
        self.assertEqual(birth.date, '1900-01-01')
        self.assertEqual(birth.place_text, 'Example County')
        self.assertEqual(plan.warnings, [])
        self._applied_lints_clean(ADVERSARIAL / 'minimal-555.ged')

    def test_preserve_extensions_person_and_variant(self) -> None:
        plan = self._plan(ADVERSARIAL / 'preserve-extensions.ged')
        self.assertEqual(len(plan.stubs), 1)
        stub = plan.stubs[0]
        self.assertEqual(stub.name, 'Zoë Fiction')
        self.assertEqual(stub.variants, ['Zed Alternative'])
        # A DEAT is present, so he is not guessed to be living.
        self.assertEqual(stub.living, 'false')

    def test_preserve_extensions_two_births_dated_one_sets_stub(self) -> None:
        plan = self._plan(ADVERSARIAL / 'preserve-extensions.ged')
        births = self._of(plan, 'birth')
        self.assertEqual(len(births), 2)
        self.assertEqual(sorted(bool(b.date) for b in births), [False, True])
        # Both BIRTs become claims. The stub's provisional `birth:` is the most
        # exact readable one (#449); the unreadable first BIRT cannot blank it.
        self.assertEqual(plan.stubs[0].birth, '2000-01-01')
        self.assertEqual(plan.stubs[0].living, 'false')     # a DEAT is present
        self.assertEqual(len(plan.date_downgrades), 1)

    def test_preserve_extensions_open_ended_deaths(self) -> None:
        plan = self._plan(ADVERSARIAL / 'preserve-extensions.ged')
        deaths = self._of(plan, 'death')
        self.assertEqual(sorted(d.date for d in deaths), ['[..2099]', '[2099..]'])

    def test_preserve_extensions_empty_even_type_uses_tag_name(self) -> None:
        plan = self._plan(ADVERSARIAL / 'preserve-extensions.ged')
        (even,) = self._of(plan, 'event')
        self.assertEqual(even.subtype, 'even')

    def test_preserve_extensions_conc_cont_note(self) -> None:
        plan = self._plan(ADVERSARIAL / 'preserve-extensions.ged')
        (note,) = self._of(plan, 'note')
        self.assertIn('first-joined', note.value)
        self.assertEqual(note.notes, 'next line')

    def test_preserve_extensions_dangling_references_warn(self) -> None:
        result, _out, _err = self._run(ADVERSARIAL / 'preserve-extensions.ged')
        plan = self._plan(ADVERSARIAL / 'preserve-extensions.ged')
        self.assertTrue(any('@I404@' in w for w in plan.warnings), plan.warnings)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)

    def test_preserve_extensions_vendor_and_media_counted_unread(self) -> None:
        plan = self._plan(ADVERSARIAL / 'preserve-extensions.ged')
        tags = dict(plan.unread_top)
        self.assertIn('_VENDOR', tags)
        self.assertIn('OBJE', tags)
        # A person-level SOUR is counted, not read (issue #450 asks to read it).
        self.assertIn('SOUR', tags)
        self._applied_lints_clean(ADVERSARIAL / 'preserve-extensions.ged')

    def test_xref_source_a_dangling_child_warns_and_text_is_not_a_link(self) -> None:
        plan = self._plan(ADVERSARIAL / 'xref-source-a.ged')
        self.assertTrue(any('@I99@' in w for w in plan.warnings), plan.warnings)
        # The missing child makes no relationship, and the `_TEXT literal @I1@
        # and @I99@` line is never followed as a link: it is counted unread.
        self.assertEqual(plan.claims, [])
        self.assertEqual(dict(plan.unread_top).get('_TEXT'), 1)
        self._applied_lints_clean(ADVERSARIAL / 'xref-source-a.ged')

    # -- files built from bytes ----------------------------------------------

    def test_crlf_imports_same_as_lf(self) -> None:
        lf = self._plan(self._bytes('lf.ged', _CRLF_SOURCE.encode('utf-8')))
        crlf = self._plan(self._bytes(
            'crlf.ged', _CRLF_SOURCE.replace('\n', '\r\n').encode('utf-8')))

        def shape(p):
            return [(c.claim_type, c.anchor, c.date, c.value) for c in p.claims]
        self.assertEqual(shape(crlf), shape(lf))
        self.assertEqual(crlf.warnings, lf.warnings)

    def test_utf8_bom_imports(self) -> None:
        plan = self._plan(self._bytes('bom.ged', b'\xef\xbb\xbf' + _CRLF_SOURCE.encode('utf-8')))
        self.assertEqual(plan.stubs[0].name, 'Rose Hartley')
        self.assertEqual(plan.warnings, [])

    def test_utf16_both_byte_orders_refused(self) -> None:
        for name, raw in (('le.ged', b'\xff\xfe' + _CRLF_SOURCE.encode('utf-16-le')),
                          ('be.ged', b'\xfe\xff' + _CRLF_SOURCE.encode('utf-16-be'))):
            result, _out, err = self._run(self._bytes(name, raw), apply=True)
            self.assertEqual(result.exit_code, EXIT_ERRORS, name)
            self.assertIn('UTF-16', err)
            self.assertFalse((self.archive / 'people').exists())

    def test_partial_bom_refused_as_not_utf8(self) -> None:
        raw = b'\xef\xbb' + _CRLF_SOURCE.encode('utf-8')
        result, _out, err = self._run(self._bytes('partial.ged', raw), apply=True)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('not UTF-8', err)

    def test_skipped_level_warns(self) -> None:
        raw = b'0 HEAD\n1 CHAR UTF-8\n0 @I1@ INDI\n2 DATE 1900\n0 TRLR\n'
        plan = self._plan(self._bytes('skip.ged', raw))
        self.assertTrue(any('level jumps to 2' in w for w in plan.warnings), plan.warnings)

    # A `0 @I1@ INDI` defined twice is refused: covered by the dup-xref.ged
    # test above, not repeated here.

    def test_bare_level_stays_malformed_not_joined(self) -> None:
        raw = (b'0 HEAD\n1 CHAR UTF-8\n0 @I1@ INDI\n1 NAME Rose /Hartley/\n1\n'
               b'1 NOTE Kept the bible\n0 TRLR\n')
        plan = self._plan(self._bytes('bare.ged', raw))
        self.assertTrue(any('did not parse' in w and 'line 5' in w for w in plan.warnings),
                        plan.warnings)

    def test_negative_level_stays_malformed_not_joined(self) -> None:
        # A '-1 NAME x' line is a broken GEDCOM line, not note prose: it is
        # reported, so his name does not quietly become "Rose Hartley -1 NAME x".
        raw = (b'0 HEAD\n1 CHAR UTF-8\n0 @I1@ INDI\n1 NAME Rose /Hartley/\n-1 NAME x\n'
               b'1 NOTE Kept the bible\n0 TRLR\n')
        plan = self._plan(self._bytes('neg.ged', raw))
        self.assertTrue(any('did not parse' in w and 'line 5' in w for w in plan.warnings),
                        plan.warnings)
        self.assertEqual(plan.stubs[0].name, 'Rose Hartley')

    def test_every_broken_level_shape_is_reported_or_read(self) -> None:
        # Each of these is an attempt at a GEDCOM line. None may be folded into
        # the name above it: it is either read as a line or reported.
        reported = ['-1 NAME x', '-1 SEX F', '-1 @I2@ INDI', '-2 _MYTAG x', '+1 NAME x', '−1 NAME x', '–1 NAME x',
                    '\xa01 NAME x', '　1 NAME x', '\t1 NAME x', '  1 NAME x',
                    '100 NAME x', '1NAME x', '\xb2 NAME x']
        read = ['01 NAME x', '١ NAME x', '１ NAME x']
        for line in reported + read:
            with self.subTest(line=line):
                text = '0 HEAD\n0 @I1@ INDI\n1 NAME Rose /Hartley/\n' + line + '\n0 TRLR\n'
                records, malformed, joined = gedcom_import.parse_gedcom(text)
                self.assertEqual(joined, [])
                self.assertEqual(records[1].children[0].value, 'Rose /Hartley/')
                if line in reported:
                    self.assertTrue(any(m.startswith('line 4') for m in malformed), malformed)
                else:
                    self.assertEqual(malformed, [])
                    self.assertEqual(records[1].children[1].value, 'x')

    def test_note_prose_starting_with_a_sign_or_number_still_joins(self) -> None:
        # A MyHeritage note line that opens with a dash, a sign or a no-break
        # space is still prose: it joins the note above it, words kept.
        for line in ('- and her sister', '-30 degrees, the well froze',
                     '–1940 they stayed on the farm', '—1918 was the year of the flu',
                     '\xa01937 the farm was sold', '+2 more cousins in Topeka',
                     '-1920 I started school in Fairview', '+2 US Army cousins came home',
                     '\xa01918 A hard year for the family',
                     '-1942 WWII took both brothers',
                     '–1920 MOVED TO KANSAS WITH HER PARENTS',
                     '+2 USA trips after the war', '\xa01945 FDR died that spring'):
            with self.subTest(line=line):
                text = '0 HEAD\n0 @I1@ INDI\n1 NOTE Rose kept the bible\n' + line + '\n0 TRLR\n'
                records, malformed, joined = gedcom_import.parse_gedcom(text)
                self.assertEqual(malformed, [])
                self.assertEqual(joined, [4])
                self.assertEqual(records[1].children[0].value,
                                 'Rose kept the bible ' + line.strip())

    def test_sign_prose_keeps_the_numbered_prose_rule_on(self) -> None:
        # A "-5 below zero" line may be the file's only level-less line. It
        # must still mark the file as having broken lines, so "2 brothers
        # nearby" under the note joins too instead of becoming an unknown
        # BROTHERS tag whose words appear in no warning.
        for first in ('-5 below zero that January', '-1920 I started school',
                      '-1 NAME x'):
            with self.subTest(first=first):
                text = ('0 HEAD\n0 @I1@ INDI\n1 NAME Rose /Hartley/\n'
                        '1 NOTE She remembered the winter of 1936\n' + first + '\n'
                        '2 brothers nearby helped with the cattle\n0 TRLR\n')
                records, malformed, joined = gedcom_import.parse_gedcom(text)
                note = records[1].children[1]
                self.assertEqual(note.children, [])
                self.assertTrue(note.value.endswith('helped with the cattle'), note.value)
                self.assertIn(6, joined)


# ── A newer download of a tree already imported (the plan; apply refuses) ─────

SMALL_V2 = FIXTURES / 'small-v2.ged'


def _lf(path: Path) -> str:
    """A fixture's text with LF line endings, whatever the checkout wrote."""
    return path.read_bytes().decode('utf-8').replace('\r\n', '\n')


class _ReimportCase(_ArchiveCase):
    """An archive that already holds `small.ged`, the earlier download.

    Every expectation below comes from the contract (TOOLING §13a2, "A newer
    download of a tree already imported"), worked out by hand from the two
    fixture files. `small-v2.ged` is `small.ged` a year later: Rose Hartley's
    birth year corrected, Thomas given a residence, Ada's birth made exact,
    Mary Ann Cole respelled Coles, Silas's birth year made exact, Rose Cole's
    residence removed, Frances given a death, the Cole divorce year changed,
    and one new person, Walter Hartley, a child of Thomas and Rose."""

    def setUp(self) -> None:
        super().setUp()
        result, _out, _err = self._run(SMALL, apply=True)
        self.assertTrue(result.data['applied'])
        self.sid1 = result.data['source_id']
        self.record1 = next((self.archive / 'sources' / 'other').glob('*.md'))
        self.filed1 = next((self.archive / 'documents' / 'gedcom').glob('*.ged'))

    # Building newer downloads.

    def _ged(self, name: str, text: str) -> Path:
        path = Path(self._tmp.name) / name
        path.write_bytes(text.encode('utf-8'))
        return path

    def _edited(self, name: str, base: Path, *edits: tuple[str, str]) -> Path:
        """`base` with each (old, new) applied once. An `old` that is missing
        or repeated fails the test, so a fixture drift is loud."""
        text = _lf(base)
        for old, new in edits:
            self.assertEqual(text.count(old), 1, f'{old!r} must occur once in {base.name}')
            text = text.replace(old, new)
        return self._ged(name, text)

    def _same_tree_redownloaded(self) -> Path:
        """`small.ged` downloaded again later: only the export date differs."""
        return self._edited('small-again.ged', SMALL,
                            ('1 DATE 1 JUL 2026', '1 DATE 1 JUL 2027'))

    @staticmethod
    def _renumbered(text: str) -> str:
        """Every person and family given a new record number, as a program
        that renumbers on export would."""
        import re
        text = re.sub(r'@I(\d+)@', lambda m: f'@P{100 + int(m.group(1))}@', text)
        return re.sub(r'@F(\d+)@', lambda m: f'@G{m.group(1)}@', text)

    # Reading the archive and the plan.

    def _plan(self, ged: Path, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return gedcom_import.build_plan(self.archive, self.config, ged, **kwargs)

    def _person_path(self, name: str) -> Path:
        for path in sorted((self.archive / 'people').rglob('*.md')):
            if read_record(path)['meta'].get('name') == name:
                return path
        self.fail(f'no person record named {name}')

    def _pid(self, name: str) -> str:
        return str(read_record(self._person_path(name))['meta']['id'])

    def _v1_claim(self, ctype: str, fragment: str) -> dict:
        found = [c for c in read_record(self.record1)['claims']
                 if c['type'] == ctype and fragment in c['value']]
        self.assertEqual(len(found), 1, f'{ctype} claim containing {fragment!r}')
        return found[0]

    def _edit_record1(self, old: str, new: str) -> None:
        text = self.record1.read_text(encoding='utf-8')
        self.assertEqual(text.count(old), 1, f'{old!r} must occur once in the record')
        self.record1.write_text(text.replace(old, new), encoding='utf-8')

    def _fact(self, plan, ctype: str, name: str, fragment: str = ''):
        pid = self._pid(name)
        found = [f for f in plan.facts
                 if f.claim.claim_type == ctype and pid in f.claim.persons
                 and fragment in f.claim.value]
        self.assertEqual(len(found), 1, f'{ctype} fact naming {name}')
        return found[0]

    def _lose_the_cache(self) -> None:
        """Lose the cache and the import's list in notes/ too, so what is
        left is the records alone (the harder case)."""
        shutil.rmtree(self.archive / '.cache' / 'gedcom_import')
        shutil.rmtree(self.archive / 'notes' / 'gedcom-imports', ignore_errors=True)


class ReimportPlanTestCase(_ReimportCase):
    # The same file, or the same tree with nothing changed.

    def test_same_file_is_refused_even_when_the_cache_is_gone(self) -> None:
        # The cache is not backed up, so on a new computer the record itself
        # has to say "you already have this file".
        self._lose_the_cache()
        before = _tree_digest(self.archive)
        result, _out, err = self._run(SMALL)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('already imported', err)
        self.assertIn(self.sid1, err)
        self.assertRegex(err, r'\d{4}-\d{2}-\d{2}')
        self.assertEqual(_tree_digest(self.archive), before)

    def test_same_tree_downloaded_again_plans_nothing(self) -> None:
        before = _tree_digest(self.archive)
        result, out, _err = self._run(self._same_tree_redownloaded())
        self.assertEqual(result.data['mode'], 'update')
        self.assertEqual(result.data['update_of'], [self.sid1])
        self.assertEqual(result.data['matched'], 10)
        self.assertEqual(result.data['new_people'], 0)
        self.assertEqual(result.data['unmatched'], [])
        self.assertEqual(result.data['same'], 23)
        self.assertEqual(result.data['claims'], 0)
        self.assertEqual(result.data['questions'], 0)
        self.assertIn('Nothing new', out)
        self.assertEqual(_tree_digest(self.archive), before)

    # The newer download.

    def test_newer_download_recognised(self) -> None:
        result, out, _err = self._run(SMALL_V2)
        self.assertEqual(result.data['mode'], 'update')
        self.assertEqual(result.data['update_of'], [self.sid1])
        self.assertEqual(result.data['matched'], 10)       # every person of the earlier download
        self.assertEqual(result.data['new_people'], 1)
        self.assertEqual(result.data['unmatched'], [])
        self.assertEqual(result.data['duplicates'], [])
        self.assertIn('newer download', out)
        self.assertIn(self.sid1, out)

    def test_counts_of_what_changed(self) -> None:
        # 26 statements in the newer file: 18 unchanged, 4 new (Thomas's
        # residence, Frances's death, Walter's birth, Walter's parents), 2 that
        # add detail (Ada's and Silas's exact birth), 2 changed (Rose's birth
        # year, the Cole divorce year).
        result, _out, _err = self._run(SMALL_V2)
        self.assertEqual(result.data['same'], 18)
        self.assertEqual(result.data['new'], 4)
        self.assertEqual(result.data['more_detail'], 2)
        self.assertEqual(result.data['changed'], 2)
        self.assertEqual(result.data['questions'], 2)
        self.assertEqual(result.data['claims'], 8)          # only these would be added
        self.assertEqual(result.data['held_statements'], 0)

    def test_dry_run_writes_nothing(self) -> None:
        before = _tree_digest(self.archive)
        self._run(SMALL_V2)
        self.assertEqual(_tree_digest(self.archive), before)

    def test_no_duplicate_people_planned(self) -> None:
        plan = self._plan(SMALL_V2)
        self.assertEqual([s.name for s in plan.stubs], ['Walter Hartley'])

    def test_changed_birth_planned_with_question(self) -> None:
        plan = self._plan(SMALL_V2)
        fact = self._fact(plan, 'birth', 'Rose Hartley')
        self.assertEqual(fact.outcome, 'changed')
        old = self._v1_claim('birth', 'Rose Hartley')
        self.assertEqual(fact.against, [old['id']])
        self.assertEqual(fact.claim.date, '1879-01-12')
        headings = [q.heading for q in plan.questions]
        self.assertIn('When and where was Rose Hartley born?', headings)
        question = next(q for q in plan.questions if 'Rose Hartley born' in q.heading)
        self.assertIn(old['id'], question.refs)
        self.assertIn(self._pid('Rose Hartley'), question.refs)

    def test_more_detail_is_not_a_disagreement(self) -> None:
        plan = self._plan(SMALL_V2)
        for name, date in (('Ada Hartley', '1901-03-03'), ('Silas Cole', '1849')):
            fact = self._fact(plan, 'birth', name)
            self.assertEqual(fact.outcome, 'more detail', name)
            self.assertEqual(fact.against, [])
            self.assertEqual(fact.claim.date, date)

    def test_unchanged_statements_are_not_planned_again(self) -> None:
        plan = self._plan(SMALL_V2)
        self.assertEqual(self._fact(plan, 'occupation', 'Thomas Hartley').outcome, 'same')
        self.assertEqual(self._fact(plan, 'death', 'Rose Hartley').outcome, 'same')
        planned = {(c.claim_type, tuple(c.persons)) for c in plan.claims}
        self.assertNotIn(('occupation', (self._pid('Thomas Hartley'),)), planned)
        self.assertNotIn(('death', (self._pid('Rose Hartley'),)), planned)

    def test_couple_change(self) -> None:
        plan = self._plan(SMALL_V2)
        fact = self._fact(plan, 'divorce', 'Silas Cole')
        self.assertEqual(fact.outcome, 'changed')
        self.assertEqual(fact.against, [self._v1_claim('divorce', 'Silas Cole')['id']])
        self.assertIn('When did Silas Cole and Mary Ann Cole divorce?',
                      [q.heading for q in plan.questions])
        # The marriage is worded differently only because the wife's name was
        # respelled. Its date did not change, so it is not planned again.
        self.assertEqual(self._fact(plan, 'marriage', 'Silas Cole').outcome, 'same')

    def test_new_child_is_tied_to_the_parents_already_on_file(self) -> None:
        plan = self._plan(SMALL_V2)
        walter = plan.stubs[0]
        rel = [f for f in plan.facts if f.claim.claim_type == 'relationship'
               and f.claim.roles['child'] == walter.pid]
        self.assertEqual(len(rel), 1)
        self.assertEqual(rel[0].outcome, 'new')
        self.assertEqual(rel[0].claim.roles['parent'],
                         [self._pid('Thomas Hartley'), self._pid('Rose Hartley')])

    def test_statement_gone_from_the_newer_file_is_listed_not_touched(self) -> None:
        before = self.record1.read_bytes()
        result, out, _err = self._run(SMALL_V2)
        self.assertEqual(len(result.data['gone_facts']), 1)
        self.assertIn('Rose Cole', result.data['gone_facts'][0])
        self.assertIn('residence', result.data['gone_facts'][0])
        self.assertIn('No longer in the newer file', out)
        self.assertEqual(self.record1.read_bytes(), before)

    def test_death_in_the_newer_file_is_listed_never_written(self) -> None:
        frances = self._person_path('Frances Thorsson')
        before = frances.read_bytes()
        result, out, _err = self._run(SMALL_V2)
        pid = self._pid('Frances Thorsson')
        self.assertEqual(len(result.data['death_now']), 1)
        self.assertIn(f'fha person set-living {pid} false', result.data['death_now'][0])
        self.assertIn(f'fha person set-living {pid} false', out)
        self.assertEqual(frances.read_bytes(), before)

    def test_name_respelled_in_the_newer_file_is_listed_never_written(self) -> None:
        mary = self._person_path('Mary Ann Cole')
        before = mary.read_bytes()
        result, out, _err = self._run(SMALL_V2)
        self.assertEqual(len(result.data['name_changes']), 1)
        self.assertIn('Mary Ann Coles', result.data['name_changes'][0])
        self.assertIn('your records keep their names', out)
        self.assertEqual(mary.read_bytes(), before)

    # Nothing he edited is argued with.

    def test_his_own_correction_matching_the_newer_file_plans_nothing(self) -> None:
        import claim as claim_mod
        cid = self._v1_claim('birth', 'Silas Cole')['id']
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rr = claim_mod.run_claim(self.archive, claim_id=cid, date='1849')
        self.assertEqual(rr.exit_code, EXIT_CLEAN)
        plan = self._plan(SMALL_V2)
        self.assertEqual(self._fact(plan, 'birth', 'Silas Cole').outcome, 'same')

    def test_a_date_he_corrected_is_not_contradicted_by_an_unchanged_download(self) -> None:
        # He found the real record and corrected Rose's birth to 1877. The tree
        # on Ancestry still says 1878, exactly as it did last year. The newer
        # download says nothing new, so nothing is planned against his edit.
        self._edit_record1("date: '1878-01-12'", "date: '1877-01-12'")
        for lose_copy in (False, True):
            if lose_copy:
                self.filed1.unlink()
            result, _out, _err = self._run(self._same_tree_redownloaded())
            self.assertEqual(result.data['mode'], 'update', lose_copy)
            self.assertEqual(result.data['changed'], 0, lose_copy)
            self.assertEqual(result.data['questions'], 0, lose_copy)
            self.assertEqual(result.data['claims'], 0, lose_copy)

    def test_a_statement_he_reworded_is_not_planned_again(self) -> None:
        self._edit_record1(
            "- value: 'Rose Hartley: born 12 JAN 1878, Springfield, Illinois (GEDCOM @I1@)'",
            '- value: Rose Hartley was born 12 January 1877 in Springfield (the bible page)')
        self._edit_record1("date: '1878-01-12'", "date: '1877-01-12'")
        result, _out, _err = self._run(self._same_tree_redownloaded())
        self.assertEqual(result.data['matched'], 10)
        self.assertEqual(result.data['claims'], 0)
        self.assertEqual(result.data['changed'], 0)

    def test_a_parent_he_corrected_is_not_contradicted_by_an_unchanged_download(self) -> None:
        # He decided Ada's father was Silas, not Thomas, and re-pointed the
        # claim. The tree still says Thomas, as it did before: nothing new.
        thomas, silas = self._pid('Thomas Hartley'), self._pid('Silas Cole')
        text = self.record1.read_text(encoding='utf-8')
        start = text.index("- value: Ada Hartley is a child of Thomas Hartley")
        end = text.index('- value:', start + 1)
        block = text[start:end].replace(thomas, silas)
        self.assertNotEqual(block, text[start:end])
        self.record1.write_text(text[:start] + block + text[end:], encoding='utf-8')
        result, _out, _err = self._run(self._same_tree_redownloaded())
        self.assertEqual(result.data['mode'], 'update')
        self.assertEqual(result.data['changed'], 0)
        self.assertEqual(result.data['claims'], 0)

    def test_a_parent_the_newer_file_replaces_is_a_question(self) -> None:
        # The tree now gives the Hartley children Silas Cole as their father.
        newer = self._edited('father.ged', SMALL, ('1 HUSB @I2@\n', '1 HUSB @I6@\n'))
        plan = self._plan(newer)
        ada = self._pid('Ada Hartley')
        fact = next(f for f in plan.facts if f.claim.claim_type == 'relationship'
                    and f.claim.roles['child'] == ada)
        self.assertEqual(fact.outcome, 'changed')
        old = self._v1_claim('relationship', 'Ada Hartley is a child')
        self.assertEqual(fact.against, [old['id']])
        headings = [q.heading for q in plan.questions]
        self.assertIn("Who were Ada Hartley's parents?", headings)
        self.assertIn('Who adopted Ben Hartley?', headings)

    def test_a_name_he_corrected_keeps_its_match_and_its_name(self) -> None:
        mary = self._person_path('Mary Ann Cole')
        text = mary.read_text(encoding='utf-8')
        mary.write_text(text.replace('Mary Ann Cole', 'Mary Anne Coole'), encoding='utf-8')
        before = mary.read_bytes()
        for lose_evidence in (False, True):
            if lose_evidence:
                # No cache and no filed copy: only the records are left.
                self._lose_the_cache()
                self.filed1.unlink()
            result, _out, _err = self._run(self._same_tree_redownloaded())
            self.assertEqual(result.data['matched'], 10, lose_evidence)
            self.assertEqual(result.data['new_people'], 0, lose_evidence)
            self.assertEqual(result.data['claims'], 0, lose_evidence)
            self.assertEqual(mary.read_bytes(), before)
            if not lose_evidence:
                # Both downloads call her Mary Ann Cole: the tree did not
                # change, so there is no name difference to report.
                self.assertEqual(result.data['name_changes'], [])

    def test_a_statement_he_rejected_is_not_brought_back(self) -> None:
        import claim as claim_mod
        cid = self._v1_claim('death', 'Rose Hartley')['id']
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rr = claim_mod.run_claim(self.archive, claim_id=cid, status='rejected',
                                     notes='the 1950 death is another Rose')
        self.assertEqual(rr.exit_code, EXIT_CLEAN)
        plan = self._plan(SMALL_V2)
        fact = self._fact(plan, 'death', 'Rose Hartley')
        self.assertEqual(fact.outcome, 'decided')
        self.assertNotIn(fact.claim, plan.claims)

    def test_a_statement_he_deleted_is_not_brought_back(self) -> None:
        text = self.record1.read_text(encoding='utf-8')
        start = text.index("- value: 'Thomas Hartley: occupation Bookkeeper")
        end = text.index('- value:', start + 1)
        self.record1.write_text(text[:start] + text[end:], encoding='utf-8')
        plan = self._plan(SMALL_V2)
        fact = self._fact(plan, 'occupation', 'Thomas Hartley')
        self.assertEqual(fact.outcome, 'decided')
        self.assertNotIn(fact.claim, plan.claims)

    def test_a_person_he_removed_is_not_added_back(self) -> None:
        ben_pid = self._pid('Ben Hartley')
        self._person_path('Ben Hartley').unlink()
        result, out, _err = self._run(SMALL_V2)
        self.assertEqual(len(result.data['removed_people']), 1)
        self.assertIn('@I4@', result.data['removed_people'][0])
        self.assertIn('not added back', out)
        plan = self._plan(SMALL_V2)
        self.assertEqual([s.name for s in plan.stubs], ['Walter Hartley'])
        for claim in plan.claims:
            self.assertNotIn(ben_pid, claim.persons)
            self.assertNotIn('Ben Hartley', claim.value)

    def test_a_merged_person_reads_as_the_survivor(self) -> None:
        # SPEC §9: the merged record stays as a tombstone that points at the
        # survivor, and every claim naming it counts for the survivor.
        old_path = self._person_path('Rose Cole')
        old_pid = self._pid('Rose Cole')
        survivor = 'p-bbbbbbbbbb'
        (self.archive / 'people' / f'cole__rose_{survivor}.md').write_text(
            f'---\nid: {survivor}\naliases: [{survivor}, Rose Cole]\nname: Rose Cole\n'
            'sex: F\nliving: false\nbirth: 1878\ntier: stub\n---\n', encoding='utf-8')
        text = old_path.read_text(encoding='utf-8')
        text = text.replace('tier: stub', f'status: merged\nmerged_into: {survivor}\n'
                            'merge_reason: same person\nmerged_date: 2026-11-01\ntier: stub')
        old_path.unlink()
        (old_path.parent / f'MERGED-INTO-{survivor}__{old_path.name}').write_text(
            text, encoding='utf-8')
        plan = self._plan(SMALL_V2)
        self.assertEqual(plan.mode, 'update')
        self.assertEqual(len(plan.matches), 10)
        self.assertEqual(plan.matches['@I7@'].pid, survivor)
        self.assertEqual([s.name for s in plan.stubs], ['Walter Hartley'])
        people = {pid for f in plan.facts for pid in f.claim.persons}
        self.assertIn(survivor, people)
        self.assertNotIn(old_pid, people)
        # Her birth is still the statement already on file.
        birth = [f for f in plan.facts
                 if f.claim.claim_type == 'birth' and f.claim.persons == [survivor]]
        self.assertEqual([f.outcome for f in birth], ['same'])

    # Who is who: matched for certain, or listed with the reason.

    def test_cache_lost_still_matches(self) -> None:
        self._lose_the_cache()
        result, _out, _err = self._run(SMALL_V2)
        self.assertEqual(result.data['mode'], 'update')
        self.assertEqual(result.data['matched'], 10)
        self.assertEqual(result.data['new_people'], 1)
        self.assertEqual(result.data['claims'], 8)

    def test_cache_and_filed_copy_lost_still_matches_from_the_records(self) -> None:
        self._lose_the_cache()
        self.filed1.unlink()
        result, out, _err = self._run(SMALL_V2)
        self.assertEqual(result.data['mode'], 'update')
        self.assertEqual(result.data['matched'], 10)
        self.assertEqual(result.data['new_people'], 1)
        self.assertEqual(result.data['changed'], 2)
        self.assertIn('could not be read', out)       # says what it compared against

    def test_record_number_reused_for_someone_else_is_not_matched(self) -> None:
        # @I4@ was Ben Hartley. In this download @I4@ is Walter Hartley, born
        # 1899: a brother given Ben's freed number, or Ben renamed? The file
        # cannot say, so nothing is planned for either reading.
        ben_pid = self._pid('Ben Hartley')
        reused = self._edited(
            'reused.ged', SMALL,
            ('1 NAME Ben /Hartley/\n1 SEX M\n1 BIRT\n2 DATE INT 1902 (spring after the flood)\n',
             '1 NAME Walter /Hartley/\n1 SEX M\n1 BIRT\n2 DATE 1899\n'))
        result, out, _err = self._run(reused)
        self.assertEqual(result.data['mode'], 'update')
        self.assertEqual(result.data['matched'], 9)
        self.assertEqual(result.data['new_people'], 0)
        self.assertEqual(len(result.data['unmatched']), 1)
        line = result.data['unmatched'][0]
        self.assertIn('@I4@', line)
        self.assertIn('Walter Hartley', line)
        self.assertIn('Ben Hartley', line)
        self.assertIn('Not matched for certain', out)
        plan = self._plan(reused)
        self.assertNotIn('@I4@', plan.matches)
        self.assertEqual(plan.stubs, [])
        self.assertEqual(plan.claims, [])                  # nothing guessed onto Ben
        for fact in plan.facts:
            self.assertNotIn(ben_pid, fact.claim.persons)
        # Walter's birth and his tie to his parents are held with him.
        self.assertEqual(result.data['held_statements'], 2)
        # Ben is not reported as gone either: his number is still in the file.
        self.assertEqual(result.data['gone_people'], [])
        self.assertEqual(result.data['gone_facts'], [])

    def test_two_people_who_swapped_numbers_are_not_matched_by_number(self) -> None:
        ada = '1 NAME Ada /Hartley/\n1 SEX F\n1 BIRT\n2 DATE AFT 1900\n'
        ben = '1 NAME Ben /Hartley/\n1 SEX M\n1 BIRT\n2 DATE INT 1902 (spring after the flood)\n'
        swapped = self._edited('swapped.ged', SMALL, (ada, '<ADA>'), (ben, ada), ('<ADA>', ben))
        plan = self._plan(swapped)
        self.assertEqual(plan.mode, 'update')
        self.assertEqual(len(plan.matches), 8)
        self.assertNotIn('@I3@', plan.matches)
        self.assertNotIn('@I4@', plan.matches)
        self.assertEqual(plan.stubs, [])
        self.assertEqual(plan.claims, [])
        # Each is listed with the one person they most likely are.
        self.assertEqual(plan.held['@I3@'].likely.pid, self._pid('Ben Hartley'))
        self.assertEqual(plan.held['@I4@'].likely.pid, self._pid('Ada Hartley'))

    def test_renumbered_tree_is_recognised_and_nobody_is_guessed(self) -> None:
        renumbered = self._ged('renumbered.ged', self._renumbered(_lf(SMALL_V2)))
        result, out, _err = self._run(renumbered)
        self.assertEqual(result.data['mode'], 'update')    # not 11 new people
        self.assertEqual(result.data['matched'], 0)
        # Walter is in neither download's shared people: he is new.
        self.assertEqual(result.data['new_people'], 1)
        # The ten people of the earlier download are each listed with the one
        # person they most likely are, and nothing is planned for them.
        self.assertEqual(len(result.data['unmatched']), 10)
        self.assertEqual(result.data['unmatched_likely'], 10)
        self.assertEqual(result.data['changed'], 0)
        self.assertEqual(result.data['questions'], 0)
        self.assertIn('record numbers', out)
        plan = self._plan(renumbered)
        self.assertEqual([s.name for s in plan.stubs], ['Walter Hartley'])
        self.assertEqual(plan.held['@P101@'].likely.pid, self._pid('Rose Hartley'))
        # Respelled and renumbered: part of the name and the same birth date.
        self.assertEqual(plan.held['@P105@'].likely.pid, self._pid('Mary Ann Cole'))
        # Only Walter's own birth is planned; his parents are not certain.
        self.assertEqual([c.claim_type for c in plan.claims], ['birth'])

    def test_statements_now_naming_two_people_stop_the_match(self) -> None:
        # He decided the tree's 1950 death was really Thomas's and re-pointed
        # that one claim. The earlier download's @I1@ now stands for two
        # people, so the plan does not pick one.
        rose, thomas = self._pid('Rose Hartley'), self._pid('Thomas Hartley')
        text = self.record1.read_text(encoding='utf-8')
        start = text.index("- value: 'Rose Hartley: died ABT 1950")
        end = text.index('- value:', start + 1)
        block = text[start:end].replace(f'[[{rose}|Rose Hartley]]', f'[[{thomas}|Thomas Hartley]]')
        self.record1.write_text(text[:start] + block + text[end:], encoding='utf-8')
        self._lose_the_cache()
        plan = self._plan(SMALL_V2)
        self.assertNotIn('@I1@', plan.matches)
        self.assertIn('more than one person', plan.held['@I1@'].reason)
        for claim in plan.claims:
            self.assertNotIn(rose, claim.persons)

    def test_different_tree_imports_fresh(self) -> None:
        # Another family's tree that happens to use the same record numbers.
        other = self._ged('bradford.ged', '\n'.join([
            '0 HEAD', '1 SOUR OtherApp', '1 CHAR UTF-8',
            '0 @I1@ INDI', '1 NAME George /Bradford/', '1 SEX M', '1 BIRT', '2 DATE 1861',
            '0 @I2@ INDI', '1 NAME Clara /Frost/', '1 SEX F', '1 BIRT', '2 DATE 1866',
            '0 @I3@ INDI', '1 NAME Louisa /Denton/', '1 SEX F',
            '0 @F1@ FAM', '1 HUSB @I1@', '1 WIFE @I2@', '1 CHIL @I3@',
            '0 TRLR', '']))
        result, out, _err = self._run(other)
        self.assertEqual(result.data['mode'], 'new')
        self.assertEqual(result.data['persons'], 3)
        self.assertEqual(result.data['claims'], 3)
        self.assertIn('does not look like a newer download', out)

    def test_another_program_file_reusing_the_first_number_imports_fresh(self) -> None:
        # `@I1@` is Rose Hartley here and Briar Fiction in this one-person file.
        result, _out, _err = self._run(ADVERSARIAL / 'minimal-551.ged')
        self.assertEqual(result.data['mode'], 'new')
        self.assertEqual(result.data['persons'], 1)

    def test_as_new_tree_forces_a_separate_import(self) -> None:
        result, _out, _err = self._run(SMALL_V2, as_new_tree=True)
        self.assertEqual(result.data['mode'], 'new')
        self.assertEqual(result.data['persons'], 11)
        self.assertEqual(result.data['claims'], 26)

    def test_as_new_tree_still_refuses_the_very_same_file(self) -> None:
        self._lose_the_cache()
        before = _tree_digest(self.archive)
        result, _out, err = self._run(SMALL, as_new_tree=True, apply=True)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('already imported', err)
        self.assertEqual(_tree_digest(self.archive), before)

    def test_a_tree_held_as_two_separate_copies_updates_one_of_them(self) -> None:
        # The first download was filed last year; the second, today, as a
        # separate tree (every person a second time).
        text = self.record1.read_text(encoding='utf-8')
        created = next(ln for ln in text.splitlines() if ln.startswith('created: '))
        self.record1.write_text(text.replace(created, 'created: 2025-07-02'), encoding='utf-8')
        result, _out, _err = self._run(SMALL_V2, as_new_tree=True, apply=True)
        self.assertTrue(result.data['applied'])
        sid2 = result.data['source_id']
        again = self._edited('v2-again.ged', SMALL_V2,
                             ('1 DATE 1 JUL 2027', '1 DATE 1 JUL 2028'))
        result, out, _err = self._run(again)
        # It updates the newer copy, whose eleven people it matches, and does
        # not plan a third set of people.
        self.assertEqual(result.data['mode'], 'update')
        self.assertEqual(result.data['update_of'], [sid2])
        self.assertEqual(result.data['matched'], 11)
        self.assertEqual(result.data['new_people'], 0)
        self.assertEqual(result.data['unmatched'], [])
        self.assertIn('separate copy', out)                # the other one is named
        self.assertIn(self.sid1, out)

    def test_an_earlier_import_that_cannot_be_read_is_said_out_loud(self) -> None:
        # Without the warning this would quietly plan all ten people again.
        text = self.record1.read_text(encoding='utf-8')
        self.record1.write_text(text.replace('## Claims\n```yaml\n',
                                             '## Claims\n```yaml\n- value: [broken\n'),
                                encoding='utf-8')
        result, _out, err = self._run(SMALL_V2)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertIn('could not be read', err)
        self.assertIn(self.record1.name, err)

    def test_as_new_tree_flag_reaches_the_command_line(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = gedcom_import._standalone_main(
                [str(SMALL_V2), '--as-new-tree', '--root', str(self.archive)])
        self.assertEqual(rc, EXIT_WARNINGS)
        self.assertIn('Person stubs (people/stubs/): 11', out.getvalue())

    # The plan itself.

    def test_plan_is_the_same_text_every_time(self) -> None:
        # Two runs over the same archive and the same file print the same
        # plan, byte for byte: no id is made up for a plan that writes nothing.
        out_a = Path(self._tmp.name) / 'plan-a.txt'
        out_b = Path(self._tmp.name) / 'plan-b.txt'
        with mock.patch.object(gedcom_import, 'mint_ids',
                               side_effect=AssertionError('an update plan mints no ids')):
            _r1, stdout_a, stderr_a = self._run(SMALL_V2, plan_out=str(out_a))
            # The cache is only a speed-up: losing it changes nothing here.
            self._lose_the_cache()
            _r2, stdout_b, stderr_b = self._run(SMALL_V2, plan_out=str(out_b))
        self.assertEqual(stdout_a.replace(str(out_a), ''), stdout_b.replace(str(out_b), ''))
        self.assertEqual(stderr_a, stderr_b)
        self.assertEqual(out_a.read_bytes(), out_b.read_bytes())
        self.assertIn('Rose Hartley, birth', out_a.read_text(encoding='utf-8'))

    def test_plan_reads_plainly(self) -> None:
        _result, out, _err = self._run(SMALL_V2)
        self.assertIn('People: 10 already in your archive, 1 new', out)
        self.assertIn('Changed (2)', out)
        self.assertIn('Rose Hartley, birth: your archive has 12 January 1878, Springfield, '
                      'Illinois; the newer download says 12 January 1879, Springfield, Illinois',
                      out)
        self.assertIn('New people (1)', out)
        self.assertIn('Walter Hartley', out)
        self.assertIn('run the same command again with --apply', out)

    def test_question_text_names_both_claims_and_the_person(self) -> None:
        plan = self._plan(SMALL_V2)
        fact = self._fact(plan, 'birth', 'Rose Hartley')
        old = self._v1_claim('birth', 'Rose Hartley')
        question = next(q for q in plan.questions if q.fact is fact)
        block = gedcom_import._reimport_question_block(question, new_cid='c-newnewnew1',
                                                       today='2027-07-02')
        lines = block.splitlines()
        self.assertEqual(lines[0], '## Q: When and where was Rose Hartley born?')
        self.assertEqual(lines[1:3], ['- origin: tool', '- status: open'])
        self.assertEqual(
            lines[3], f'- refs: [c-newnewnew1, {old["id"]}, {self._pid("Rose Hartley")}]')
        self.assertEqual(lines[4], '- context:')
        self.assertTrue(lines[5].startswith('  - 2027-07-02 (tool): '))
        self.assertIn('12 January 1878', lines[5])
        self.assertIn('12 January 1879', lines[5])

    # Found in review.

    def _claim_block(self, claim_id: str) -> tuple[str, int, int]:
        text = self.record1.read_text(encoding='utf-8')
        start = text.rindex('- value:', 0, text.index(f'id: {claim_id}'))
        end = text.find('\n\n', start)
        return text, start, len(text) if end < 0 else end

    def test_a_spouse_he_corrected_is_not_planned_again(self) -> None:
        # He decided Rose Hartley's husband was Silas, not Thomas, and
        # re-pointed the marriage. The same tree downloaded again still says
        # Thomas: the tree repeating itself, not a new marriage to add.
        thomas, silas = self._pid('Thomas Hartley'), self._pid('Silas Cole')
        claim = self._v1_claim('marriage', 'Thomas Hartley married')
        text, start, end = self._claim_block(claim['id'])
        block = text[start:end]
        self.assertEqual(block.count(thomas), 2)
        self.record1.write_text(text[:start] + block.replace(thomas, silas) + text[end:],
                                encoding='utf-8')
        plan = self._plan(self._same_tree_redownloaded())
        self.assertEqual(plan.claims, [])
        self.assertEqual(plan.questions, [])
        self.assertEqual(plan.gone_facts, [])

    def test_a_marriage_date_he_corrected_stands_when_a_spouse_is_renamed(self) -> None:
        # He corrected the marriage year by hand. The tree did not change the
        # marriage; it only gave Rose a middle name. No question against him.
        self._edit_record1("date: '1897-06-05'", "date: '1898-06-05'")
        later = self._edited('later.ged', SMALL, ('1 DATE 1 JUL 2026', '1 DATE 1 JUL 2027'),
                             ('1 NAME Rose /Hartley/', '1 NAME Rose Ellen /Hartley/'))
        plan = self._plan(later)
        self.assertEqual(self._fact(plan, 'marriage', 'Rose Hartley').outcome, 'same')
        self.assertEqual(plan.questions, [])

    def test_a_new_person_like_someone_already_matched_is_flagged(self) -> None:
        # The newer download has a second Rose Hartley, born the same day as
        # the one already on file. She is planned as new, and he is told she
        # may be the same person.
        later = self._edited(
            'later.ged', SMALL, ('1 DATE 1 JUL 2026', '1 DATE 1 JUL 2027'),
            ('0 TRLR', '0 @I99@ INDI\n1 NAME Rose /Hartley/\n1 SEX F\n1 BIRT\n'
                       '2 DATE 12 JAN 1878\n0 TRLR'))
        plan = self._plan(later)
        self.assertEqual([s.xref for s in plan.stubs], ['@I99@'])
        self.assertTrue(any(d.startswith('@I99@') and self._pid('Rose Hartley') in d
                            for d in plan.duplicates), plan.duplicates)

    def test_two_copies_filed_the_same_day_compare_with_the_closer_one(self) -> None:
        # Both downloads were filed today as separate trees. Whichever order
        # the records are read in, a newer copy of the second is compared with
        # the second, not with the first.
        result, _out, _err = self._run(SMALL_V2, as_new_tree=True, apply=True)
        sid2 = result.data['source_id']
        again = self._edited('v2-again.ged', SMALL_V2, ('1 DATE 1 JUL 2027', '1 DATE 1 JUL 2028'))
        real = gedcom_import._read_prior_imports
        for flip in (False, True):
            def read(*args, flip=flip):
                priors, unreadable = real(*args)
                return (priors[::-1] if flip else priors), unreadable
            with mock.patch.object(gedcom_import, '_read_prior_imports', read):
                plan = self._plan(again)
            self.assertEqual([p.sid for p in plan.prior], [sid2])
            self.assertEqual((plan.stubs, plan.claims), ([], []))

    def test_a_corrected_marriage_stands_when_only_the_filed_copy_is_gone(self) -> None:
        # The filed copy alone is missing (the folder is there). His
        # corrected marriage year is still not questioned by an unchanged tree.
        self._edit_record1("date: '1897-06-05'", "date: '1898-06-05'")
        self.filed1.unlink()
        result, out, _err = self._run(self._same_tree_redownloaded())
        self.assertEqual(result.data['changed'], 0)
        self.assertIn('may be listed as new', out)

    def test_codex_a_reworded_occupation_the_tree_also_corrects_is_not_new(self) -> None:
        # He corrected Bookkeeper to Carpenter here, and later in the tree too.
        self._edit_record1("Thomas Hartley: occupation Bookkeeper",
                           "Thomas Hartley: occupation Carpenter")
        later = self._edited('later.ged', SMALL, ('1 DATE 1 JUL 2026', '1 DATE 1 JUL 2027'),
                             ('1 OCCU Bookkeeper', '1 OCCU Carpenter'))
        plan = self._plan(later)
        self.assertEqual(self._fact(plan, 'occupation', 'Thomas Hartley').outcome, 'same')
        self.assertEqual(plan.claims, [])
        self.assertEqual(plan.gone_facts, [])

    def test_codex_an_earlier_record_missing_its_id_stops_the_apply(self) -> None:
        text = self.record1.read_text(encoding='utf-8')
        line = next(ln for ln in text.splitlines() if ln.startswith('id: s-'))
        self._edit_record1(line + '\n', '')
        before = _tree_digest(self.archive)
        result, _out, err = self._run(SMALL_V2, apply=True)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('could not be read', err)
        self.assertEqual(_tree_digest(self.archive), before)

    def test_codex_a_working_copy_previews_without_its_documents(self) -> None:
        (self.archive / 'WORKING_COPY').write_text('', encoding='utf-8')
        shutil.rmtree(self.archive / 'documents')
        result, out, _err = self._run(self._same_tree_redownloaded())
        self.assertEqual(result.data['mode'], 'update')
        self.assertIn('could not be read', out)

    def test_codex_a_name_the_newer_file_adds_is_listed(self) -> None:
        later = self._edited('later.ged', SMALL, ('1 DATE 1 JUL 2026', '1 DATE 1 JUL 2027'),
                             ('1 NAME Rose /Hartley/\n', '1 NAME Rose /Hartley/\n'
                                                       '1 NAME Rose /Winters/\n'))
        plan = self._plan(later)
        self.assertTrue(any('Rose Winters' in n for n in plan.name_changes), plan.name_changes)

    def test_codex_an_unreadable_date_the_tree_changed_is_a_change(self) -> None:
        # Ben's birth is "INT 1902 (spring after the flood)": a date no one
        # can read into the date field. The tree changes it; that is shown.
        later = self._edited('later.ged', SMALL, ('1 DATE 1 JUL 2026', '1 DATE 1 JUL 2027'),
                             ('2 DATE INT 1902 (spring after the flood)',
                              '2 DATE INT 1905 (spring after the fire)'))
        plan = self._plan(later)
        self.assertEqual(self._fact(plan, 'birth', 'Ben Hartley').outcome, 'changed')
        self.assertEqual(len(plan.questions), 1)
        # Unchanged, it is still the same.
        plan = self._plan(self._same_tree_redownloaded())
        self.assertEqual(self._fact(plan, 'birth', 'Ben Hartley').outcome, 'same')

    def test_an_unreadable_date_added_where_there_was_none_is_more_detail(self) -> None:
        # Thomas's occupation had a date; his death had none. The newer file
        # gives the death a date no one can read: more detail, not a question.
        later = self._edited('later.ged', SMALL, ('1 DATE 1 JUL 2026', '1 DATE 1 JUL 2027'),
                             ('1 DEAT Y\n', '1 DEAT Y\n2 DATE INT 1950 (from the bible)\n'))
        plan = self._plan(later)
        self.assertEqual(self._fact(plan, 'death', 'Thomas Hartley').outcome, 'more detail')
        self.assertEqual(plan.questions, [])

    def test_date_disagreements_are_listed_for_new_people_only(self) -> None:
        # A matched person keeps the record he already has, so "the new record
        # shows" is said only of someone the plan would add.
        later = self._edited(
            'later.ged', SMALL, ('1 DATE 1 JUL 2026', '1 DATE 1 JUL 2027'),
            ('2 DATE 12 JAN 1878\n', '2 DATE 12 JAN 1878\n1 BIRT\n2 DATE 1890\n'),
            ('0 TRLR', '0 @I99@ INDI\n1 NAME Walter /Hartley/\n1 SEX M\n1 BIRT\n'
                       '2 DATE 1905\n1 BIRT\n2 DATE 1920\n0 TRLR'))
        plan = self._plan(later)
        self.assertEqual([c.split(' ', 1)[0] for c in plan.date_conflicts], ['@I99@'])
        text = '\n'.join(gedcom_import._plan_lines(plan, applied=False))
        self.assertIn('@I99@ Walter Hartley birth', text)

    def test_apply_is_refused_while_an_earlier_import_cannot_be_read(self) -> None:
        # A slip of the keyboard in the earlier import's record must not turn
        # the newer download into a second copy of every person.
        self._edit_record1("- value: 'Thomas Hartley: occupation Bookkeeper",
                           "- value: 'Thomas Hartley's occupation Bookkeeper")
        before = _tree_digest(self.archive)
        result, _out, err = self._run(SMALL_V2, apply=True)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('could not be read', err)
        self.assertIn('Nothing was written', err)
        self.assertEqual(_tree_digest(self.archive), before)
        # Said plainly, he can still import it as a separate tree.
        result, _out, _err = self._run(SMALL_V2, apply=True, as_new_tree=True)
        self.assertTrue(result.data['applied'])

    def test_a_documents_drive_not_plugged_in_stops_the_plan(self) -> None:
        # He deleted a statement. With the drive that holds the filed copy
        # unplugged, the plan could not see that, and would offer it again.
        text, start, end = self._claim_block(
            self._v1_claim('occupation', 'Thomas Hartley')['id'])
        self.record1.write_text(text[:start] + text[end + 2:], encoding='utf-8')
        (self.archive / 'documents').rename(self.archive / 'documents-unplugged')
        result, out, err = self._run(self._same_tree_redownloaded())
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('Plug the drive back in', err)
        self.assertNotIn('Bookkeeper', out)


class ReimportMatchingTestCase(_ArchiveCase):
    """Small trees built in the test, for the cases `small.ged` cannot hold."""

    def _tree(self, name: str, people: list[tuple[str, str, str, str]],
              families: tuple[str, ...] = ()) -> Path:
        """A tiny tree. A person with an empty birth year has a name and
        nothing else: no statement of their own."""
        lines = ['0 HEAD', '1 SOUR OtherApp', '1 CHAR UTF-8']
        for xref, person, sex, born, *more in people:
            lines += [f'0 {xref} INDI', f'1 NAME {person}', f'1 SEX {sex}']
            if born:
                lines += ['1 BIRT', f'2 DATE {born}']
            lines += list(more)
        lines += list(families) + ['0 TRLR', '']
        path = Path(self._tmp.name) / name
        path.write_bytes('\n'.join(lines).encode('utf-8'))
        return path

    def _plan(self, ged: Path):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return gedcom_import.build_plan(self.archive, self.config, ged)

    def test_two_people_with_one_name_are_listed_not_guessed(self) -> None:
        first = self._tree('first.ged', [
            ('@I1@', 'John /Hartley/', 'M', '1850'),
            ('@I2@', 'John /Hartley/', 'M', '1851'),
            ('@I3@', 'Rose /Hartley/', 'F', '1878')])
        self.assertTrue(self._run(first, apply=True)[0].data['applied'])
        later = self._tree('later.ged', [
            ('@P7@', 'John /Hartley/', 'M', '1850'),
            ('@P8@', 'John /Hartley/', 'M', '1851'),
            ('@P9@', 'Rose /Hartley/', 'F', '1878')])
        plan = self._plan(later)
        self.assertEqual(plan.mode, 'update')
        self.assertEqual(plan.matches, {})
        self.assertEqual(plan.stubs, [])
        self.assertEqual(plan.claims, [])
        for xref in ('@P7@', '@P8@'):
            self.assertIsNone(plan.held[xref].likely)
            self.assertIn('more than one person', plan.held[xref].reason)
        self.assertIsNotNone(plan.held['@P9@'].likely)

    def test_a_namesake_at_a_shifted_number_is_not_matched(self) -> None:
        # The father was deleted and the exporting program closed the gap, so
        # the son now carries the father's old number. Same number, same name,
        # thirty years apart: the son's own earlier record fits better.
        first = self._tree('first.ged', [
            ('@I1@', 'John /Hartley/', 'M', '1820'),
            ('@I2@', 'John /Hartley/', 'M', '1850'),
            ('@I3@', 'Rose /Hartley/', 'F', '1878')])
        self.assertTrue(self._run(first, apply=True)[0].data['applied'])
        later = self._tree('later.ged', [
            ('@I1@', 'John /Hartley/', 'M', '1850'),
            ('@I2@', 'Rose /Hartley/', 'F', '1878')])
        plan = self._plan(later)
        self.assertEqual(plan.mode, 'update')
        self.assertEqual(plan.matches, {})
        self.assertEqual(plan.claims, [])
        son = next(p for p in (self.archive / 'people' / 'stubs').glob('*.md')
                   if read_record(p)['meta'].get('birth') == '1850')
        self.assertEqual(plan.held['@I1@'].likely.pid, read_record(son)['meta']['id'])

    def test_a_second_parent_added_is_new_not_a_disagreement(self) -> None:
        people = [('@I1@', 'Thomas /Hartley/', 'M', '1875'),
                  ('@I2@', 'Rose /Hartley/', 'F', '1878'),
                  ('@I3@', 'Ada /Hartley/', 'F', '1901')]
        first = self._tree('first.ged', people,
                           ('0 @F1@ FAM', '1 HUSB @I1@', '1 CHIL @I3@'))
        self.assertTrue(self._run(first, apply=True)[0].data['applied'])
        later = self._tree('later.ged', people,
                           ('0 @F1@ FAM', '1 HUSB @I1@', '1 WIFE @I2@', '1 CHIL @I3@'))
        plan = self._plan(later)
        self.assertEqual(len(plan.matches), 3)
        rel = [f for f in plan.facts if f.claim.claim_type == 'relationship']
        self.assertEqual([f.outcome for f in rel], ['new'])
        self.assertEqual(rel[0].against, [])
        self.assertEqual(plan.questions, [])

    def test_a_person_with_a_name_and_nothing_else(self) -> None:
        # Thomas has no statement of his own, so no claim says which record he
        # became. The import's list knows (kept in notes/, with a copy in the
        # cache). With both gone the filed copy still says he was in the
        # earlier download, and he is listed, not planned again.
        people = [('@I1@', 'Rose /Hartley/', 'F', '1878'),
                  ('@I2@', 'Thomas /Hartley/', 'M', '')]
        self.assertTrue(self._run(self._tree('first.ged', people), apply=True)[0].data['applied'])
        later = self._tree('later.ged', people + [('@I3@', 'Ada /Hartley/', 'F', '1901')])
        plan = self._plan(later)
        self.assertEqual(sorted(plan.matches), ['@I1@', '@I2@'])
        self.assertEqual([s.name for s in plan.stubs], ['Ada Hartley'])

        shutil.rmtree(self.archive / '.cache' / 'gedcom_import')
        self.assertEqual(sorted(self._plan(later).matches), ['@I1@', '@I2@'])
        shutil.rmtree(self.archive / 'notes' / 'gedcom-imports')
        plan = self._plan(later)
        self.assertEqual(sorted(plan.matches), ['@I1@'])
        self.assertIn('does not record which person record', plan.held['@I2@'].reason)
        self.assertEqual([s.name for s in plan.stubs], ['Ada Hartley'])

        # With the filed copy gone too, nothing on file says Thomas was ever in
        # the tree. He is planned as new, and flagged as a possible duplicate.
        next((self.archive / 'documents' / 'gedcom').glob('*.ged')).unlink()
        plan = self._plan(later)
        self.assertEqual([s.name for s in plan.stubs], ['Thomas Hartley', 'Ada Hartley'])
        self.assertTrue(any('Thomas Hartley' in d for d in plan.duplicates))

    def test_photo_links_follow_the_records_people_already_have(self) -> None:
        # A newer download links the same photos. They are listed under the
        # people already on file, never under a made-up placeholder.
        self.assertTrue(self._run(PHOTO_LINKS, apply=True)[0].data['applied'])
        on_file = {str(read_record(p)['meta']['id'])
                   for p in (self.archive / 'people' / 'stubs').glob('*.md')}
        later = Path(self._tmp.name) / 'photo-links-later.ged'
        later.write_bytes(PHOTO_LINKS.read_bytes() + b'\n')
        plan = self._plan(later)
        self.assertEqual(plan.mode, 'update')
        self.assertEqual(plan.stubs, [])
        self.assertTrue(plan.photo_links)
        self.assertLessEqual({lk.pid for lk in plan.photo_links}, on_file)

    def test_a_sex_that_differs_stops_a_match_by_number(self) -> None:
        first = self._tree('first.ged', [('@I1@', 'Ada /Hartley/', 'F', '1901'),
                                         ('@I2@', 'Rose /Hartley/', 'F', '1878')])
        self.assertTrue(self._run(first, apply=True)[0].data['applied'])
        later = self._tree('later.ged', [('@I1@', 'Ada /Hartley/', 'M', '1901'),
                                         ('@I2@', 'Rose /Hartley/', 'F', '1878')])
        plan = self._plan(later)
        self.assertEqual(sorted(plan.matches), ['@I2@'])
        self.assertIn('sex', plan.held['@I1@'].reason)
        self.assertEqual(plan.stubs, [])

    # Found in review.

    def _ids(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for path in sorted((self.archive / 'people').rglob('*.md')):
            meta = read_record(path)['meta']
            out.setdefault(f"{meta['name']} {meta.get('birth') or ''}".strip(),
                           []).append(str(meta['id']))
        return out

    def test_an_undated_father_is_not_matched_to_his_son_at_his_number(self) -> None:
        # The undated father was deleted in the tree and the program closed
        # the gap: his dated son now carries his number.
        first = self._tree('first.ged', [
            ('@I1@', 'John /Hartley/', 'M', '', '1 OCCU Farmer'),
            ('@I2@', 'John /Hartley/', 'M', '1850', '1 OCCU Blacksmith'),
            ('@I3@', 'Rose /Hartley/', 'F', '1878')])
        self.assertTrue(self._run(first, apply=True)[0].data['applied'])
        later = self._tree('later.ged', [
            ('@I1@', 'John /Hartley/', 'M', '1850', '1 OCCU Blacksmith'),
            ('@I2@', 'Rose /Hartley/', 'F', '1878')])
        plan = self._plan(later)
        ids = self._ids()
        self.assertNotIn('@I1@', plan.matches)
        # Held, with the son named among the people it may be.
        self.assertIn(ids['John Hartley 1850'][0], plan.held['@I1@'].about)
        self.assertEqual(plan.claims, [])

    def test_an_undated_namesake_unchanged_still_matches(self) -> None:
        # The same tree again: the undated father keeps his number and match.
        people = [('@I1@', 'John /Hartley/', 'M', '', '1 OCCU Farmer'),
                  ('@I2@', 'John /Hartley/', 'M', '1850', '1 OCCU Blacksmith'),
                  ('@I3@', 'Rose /Hartley/', 'F', '1878')]
        self.assertTrue(self._run(self._tree('first.ged', people), apply=True)[0].data['applied'])
        plan = self._plan(self._tree('later.ged',
                                     people + [('@I4@', 'Ada /Hartley/', 'F', '1901')]))
        self.assertEqual(sorted(plan.matches), ['@I1@', '@I2@', '@I3@'])
        self.assertEqual(plan.held, {})

    def test_a_statement_he_moved_to_a_brother_does_not_make_them_one(self) -> None:
        # John's only statement was an occupation. He decided it was really
        # Thomas's and moved it. That does not make John's number Thomas's.
        first = self._tree('first.ged', [
            ('@I1@', 'Thomas /Hartley/', 'M', '1875'),
            ('@I2@', 'John /Hartley/', 'M', '', '1 OCCU Farmer'),
            ('@I3@', 'Rose /Hartley/', 'F', '1878')])
        self.assertTrue(self._run(first, apply=True)[0].data['applied'])
        ids = self._ids()
        thomas, john = ids['Thomas Hartley 1875'][0], ids['John Hartley'][0]
        record = next((self.archive / 'sources' / 'other').glob('*.md'))
        text = record.read_text(encoding='utf-8')
        start = text.rindex('- value:', 0, text.index('occupation Farmer') + 1)
        end = text.find('\n\n', start)
        end = len(text) if end < 0 else end
        block = text[start:end]
        self.assertIn(john, block)
        record.write_text(text[:start] + block.replace(john, thomas) + text[end:],
                          encoding='utf-8')
        later = self._tree('later.ged', [
            ('@I1@', 'Thomas /Hartley/', 'M', '1875'),
            ('@I2@', 'John /Hartley/', 'M', '1880', '1 OCCU Farmer'),
            ('@I3@', 'Rose /Hartley/', 'F', '1878')])
        # The import's list still says who John's number is.
        shutil.rmtree(self.archive / '.cache' / 'gedcom_import')
        self.assertEqual(self._plan(later).matches['@I2@'].pid, john)
        # With the list gone too, only the claims are left, and they no
        # longer place John.
        shutil.rmtree(self.archive / 'notes' / 'gedcom-imports')
        plan = self._plan(later)
        self.assertEqual(plan.matches['@I1@'].pid, thomas)
        self.assertNotIn('@I2@', plan.matches)
        self.assertIn('@I2@', plan.held)
        self.assertFalse(any(thomas in c.persons for c in plan.claims))
        self.assertEqual(plan.questions, [])

    def test_a_parent_he_rejected_is_not_proposed_again_beside_a_new_one(self) -> None:
        # He rejected "Ada is Thomas's child". The newer download adds Rose as
        # her mother in that family. Rose is planned; Thomas must not come
        # back with her.
        people = [('@I1@', 'Thomas /Hartley/', 'M', '1875'),
                  ('@I2@', 'Rose /Hartley/', 'F', '1878'),
                  ('@I3@', 'Ada /Hartley/', 'F', '1901')]
        first = self._tree('first.ged', people, ('0 @F1@ FAM', '1 HUSB @I1@', '1 CHIL @I3@'))
        self.assertTrue(self._run(first, apply=True)[0].data['applied'])
        record = next((self.archive / 'sources' / 'other').glob('*.md'))
        text = record.read_text(encoding='utf-8')
        start = text.index('is a child of')
        at = text.index('status: suggested', start)
        record.write_text(text[:at] + 'status: rejected' + text[at + len('status: suggested'):],
                          encoding='utf-8')
        later = self._tree('later.ged', people,
                           ('0 @F1@ FAM', '1 HUSB @I1@', '1 WIFE @I2@', '1 CHIL @I3@'))
        plan = self._plan(later)
        ids = self._ids()
        thomas, rose = ids['Thomas Hartley 1875'][0], ids['Rose Hartley 1878'][0]
        rel = [c for c in plan.claims if c.claim_type == 'relationship']
        self.assertEqual([c.roles['parent'] for c in rel], [[rose]])
        self.assertNotIn(thomas, rel[0].persons)
        self.assertNotIn('Thomas', rel[0].value)
        self.assertEqual(plan.questions, [])

    def test_a_father_the_tree_adds_is_planned_beside_a_mother_he_rejected(self) -> None:
        # He rejected "Ada is Rose's child" (the wrong mother). The newer
        # download keeps Rose and adds Thomas as the father: Thomas is new.
        people = [('@I1@', 'Thomas /Hartley/', 'M', '1875'),
                  ('@I2@', 'Rose /Hartley/', 'F', '1878'),
                  ('@I3@', 'Ada /Hartley/', 'F', '1901')]
        first = self._tree('first.ged', people, ('0 @F1@ FAM', '1 WIFE @I2@', '1 CHIL @I3@'))
        self.assertTrue(self._run(first, apply=True)[0].data['applied'])
        record = next((self.archive / 'sources' / 'other').glob('*.md'))
        text = record.read_text(encoding='utf-8')
        at = text.index('status: suggested', text.index('is a child of'))
        record.write_text(text[:at] + 'status: rejected' + text[at + len('status: suggested'):],
                          encoding='utf-8')
        later = self._tree('later.ged', people,
                           ('0 @F1@ FAM', '1 HUSB @I1@', '1 WIFE @I2@', '1 CHIL @I3@'))
        plan = self._plan(later)
        ids = self._ids()
        rel = [c for c in plan.claims if c.claim_type == 'relationship']
        self.assertEqual([c.roles['parent'] for c in rel], [[ids['Thomas Hartley 1875'][0]]])

    def test_a_duplicate_he_merged_still_matches_without_the_cache(self) -> None:
        # The tree had Rose twice. He merged the two records here, and the
        # tree dropped the second. On a new computer she still matches.
        first = self._tree('first.ged', [('@I1@', 'Rose /Hartley/', 'F', '12 JAN 1878'),
                                         ('@I2@', 'Rose /Hartley/', 'F', '1878'),
                                         ('@I3@', 'Thomas /Hartley/', 'M', '1875')])
        self.assertTrue(self._run(first, apply=True)[0].data['applied'])
        roses = sorted((str(read_record(path)['meta'].get('birth')), path)
                       for path in (self.archive / 'people').rglob('*.md')
                       if read_record(path)['meta']['name'] == 'Rose Hartley')
        (_b, dup_path), (_b2, keep_path) = roses
        survivor = str(read_record(keep_path)['meta']['id'])
        text = dup_path.read_text(encoding='utf-8').replace(
            'tier: stub', f'status: merged\nmerged_into: {survivor}\n'
            'merge_reason: same person\nmerged_date: 2026-11-01\ntier: stub')
        self.assertIn('status: merged', text)
        dup_path.unlink()
        (dup_path.parent / f'MERGED-INTO-{survivor}__{dup_path.name}').write_text(
            text, encoding='utf-8')
        shutil.rmtree(self.archive / '.cache' / 'gedcom_import')
        later = self._tree('later.ged', [
            ('@I1@', 'Rose /Hartley/', 'F', '12 JAN 1878', '1 OCCU Seamstress'),
            ('@I3@', 'Thomas /Hartley/', 'M', '1875')])
        plan = self._plan(later)
        self.assertEqual(plan.matches['@I1@'].pid, survivor)
        self.assertEqual(plan.held, {})

    def test_three_generations_renumbered_by_one_are_held(self) -> None:
        # Sr (undated), Jr (1850) and III (undated). Sr is deleted in the tree
        # and the program closes the gap: Jr is now @I1@, III is @I2@. Neither
        # may land on his father's record.
        first = self._tree('first.ged', [('@I1@', 'John /Hartley/', 'M', '', '1 OCCU Farmer'),
                                         ('@I2@', 'John /Hartley/', 'M', '1850',
                                          '1 OCCU Blacksmith'),
                                         ('@I3@', 'John /Hartley/', 'M', '', '1 OCCU Miner'),
                                         ('@I4@', 'Rose /Hartley/', 'F', '1878')])
        self.assertTrue(self._run(first, apply=True)[0].data['applied'])
        later = self._tree('later.ged', [('@I1@', 'John /Hartley/', 'M', '1850',
                                          '1 OCCU Blacksmith'),
                                         ('@I2@', 'John /Hartley/', 'M', '', '1 OCCU Miner'),
                                         ('@I3@', 'Rose /Hartley/', 'F', '1878')])
        plan = self._plan(later)
        self.assertNotIn('@I1@', plan.matches)
        self.assertNotIn('@I2@', plan.matches)
        self.assertEqual(plan.claims, [])

    def test_an_undated_person_still_matches_when_a_namesake_is_deleted(self) -> None:
        # He deletes the 1850 John Hartley from the tree (a duplicate). The
        # undated John, unchanged at his own number, keeps his match.
        first = self._tree('first.ged', [('@I1@', 'John /Hartley/', 'M', '', '1 OCCU Miner'),
                                         ('@I2@', 'John /Hartley/', 'M', '1850'),
                                         ('@I3@', 'Rose /Hartley/', 'F', '1878')])
        self.assertTrue(self._run(first, apply=True)[0].data['applied'])
        later = self._tree('later.ged', [('@I1@', 'John /Hartley/', 'M', '', '1 OCCU Miner',
                                          '1 RESI', '2 PLAC Fairview'),
                                         ('@I3@', 'Rose /Hartley/', 'F', '1878')])
        plan = self._plan(later)
        self.assertEqual(sorted(plan.matches), ['@I1@', '@I3@'])
        self.assertEqual(plan.held, {})

    def test_an_undated_father_beside_a_name_only_son_still_matches(self) -> None:
        # On a new computer (no cache) the son, who has a name and nothing
        # else, is known to no record. The father, unchanged, keeps his match.
        people = [('@I1@', 'John /Hartley/', 'M', '', '1 OCCU Miner', '1 FAMS @F1@'),
                  ('@I2@', 'Rose /Hartley/', 'F', '1878', '1 FAMS @F1@'),
                  ('@I3@', 'John /Hartley/', 'M', '', '1 FAMC @F1@')]
        fam = ('0 @F1@ FAM', '1 HUSB @I1@', '1 WIFE @I2@', '1 CHIL @I3@')
        self.assertTrue(self._run(self._tree('first.ged', people, fam),
                                  apply=True)[0].data['applied'])
        shutil.rmtree(self.archive / '.cache' / 'gedcom_import')
        later = self._tree('later.ged', [people[0] + ('1 RESI', '2 PLAC Fairview')]
                           + people[1:], fam)
        plan = self._plan(later)
        self.assertIn('@I1@', plan.matches)
        self.assertTrue(any(c.claim_type == 'residence' for c in plan.claims))

    def test_a_son_shifted_onto_his_fathers_number_at_the_end_of_the_file_is_held(self) -> None:
        # The father and son are the last people in the tree. Sr is deleted
        # and the gap closed: Jr takes Sr's number and nobody has Jr's.
        first = self._tree('first.ged', [('@I1@', 'Rose /Hartley/', 'F', '1878'),
                                         ('@I2@', 'John /Hartley/', 'M', '', '1 OCCU Farmer'),
                                         ('@I3@', 'John /Hartley/', 'M', '1850',
                                          '1 OCCU Blacksmith')])
        self.assertTrue(self._run(first, apply=True)[0].data['applied'])
        later = self._tree('later.ged', [('@I1@', 'Rose /Hartley/', 'F', '1878'),
                                         ('@I2@', 'John /Hartley/', 'M', '1850',
                                          '1 OCCU Blacksmith')])
        plan = self._plan(later)
        self.assertNotIn('@I2@', plan.matches)
        self.assertEqual(plan.claims, [])

    def test_codex_a_program_id_that_disagrees_stops_a_number_match(self) -> None:
        # Two cousins of one name born a year apart, renumbered so their
        # numbers swapped. The program's own ids say who is who.
        first = self._tree('first.ged', [
            ('@I1@', 'John /Hartley/', 'M', '1880', '1 _UID AAAA1', '1 OCCU Miner'),
            ('@I2@', 'John /Hartley/', 'M', '1881', '1 _UID BBBB2', '1 OCCU Farmer')])
        self.assertTrue(self._run(first, apply=True)[0].data['applied'])
        ids = self._ids()
        later = self._tree('later.ged', [
            ('@I1@', 'John /Hartley/', 'M', '1881', '1 _UID BBBB2', '1 OCCU Farmer'),
            ('@I2@', 'John /Hartley/', 'M', '1880', '1 _UID AAAA1', '1 OCCU Miner')])
        plan = self._plan(later)
        self.assertEqual(plan.matches['@I1@'].pid, ids['John Hartley 1881'][0])
        self.assertEqual(plan.matches['@I2@'].pid, ids['John Hartley 1880'][0])
        self.assertEqual((plan.claims, plan.questions), ([], []))

    def test_codex_a_removed_parent_beside_a_kept_one_is_a_change(self) -> None:
        people = [('@I1@', 'Thomas /Hartley/', 'M', '1875'),
                  ('@I2@', 'Rose /Hartley/', 'F', '1878'),
                  ('@I3@', 'Ada /Hartley/', 'F', '1901')]
        first = self._tree('first.ged', people,
                           ('0 @F1@ FAM', '1 HUSB @I1@', '1 WIFE @I2@', '1 CHIL @I3@'))
        self.assertTrue(self._run(first, apply=True)[0].data['applied'])
        later = self._tree('later.ged', people, ('0 @F1@ FAM', '1 HUSB @I1@', '1 CHIL @I3@'))
        plan = self._plan(later)
        rel = [f for f in plan.facts if f.claim.claim_type == 'relationship']
        self.assertEqual([f.outcome for f in rel], ['changed'])
        self.assertEqual(len(plan.questions), 1)

    def test_a_cousin_of_the_same_name_given_a_birth_year_still_matches(self) -> None:
        # Two cousins named John Hartley, one born 1849, one undated. The
        # tree keeps every number and gives the undated one a year, 1850.
        first = self._tree('first.ged', [('@I1@', 'John /Hartley/', 'M', '1849'),
                                         ('@I2@', 'John /Hartley/', 'M', '', '1 OCCU Miner'),
                                         ('@I3@', 'Rose /Hartley/', 'F', '1878')])
        self.assertTrue(self._run(first, apply=True)[0].data['applied'])
        later = self._tree('later.ged', [('@I1@', 'John /Hartley/', 'M', '1849'),
                                         ('@I2@', 'John /Hartley/', 'M', '1850', '1 OCCU Miner'),
                                         ('@I3@', 'Rose /Hartley/', 'F', '1878')])
        plan = self._plan(later)
        self.assertEqual(sorted(plan.matches), ['@I1@', '@I2@', '@I3@'])
        self.assertEqual(plan.held, {})


class ReimportComparisonTestCase(unittest.TestCase):
    """The two comparisons a changed statement turns on (TOOLING §13a2)."""

    def test_dates(self) -> None:
        rel = gedcom_import._date_relation
        self.assertEqual(rel('1878-01-12', '1878-01-12'), 'eq')
        self.assertEqual(rel('', ''), 'eq')
        self.assertEqual(rel('', '1878'), 'adds')                  # a missing date filled in
        self.assertEqual(rel('1878~', '1878-01-12'), 'adds')       # about 1878 -> the day
        self.assertEqual(rel('[1900..]', '1901-03-03'), 'adds')    # after 1900 -> the day
        self.assertEqual(rel('1878-01-12', '1878'), 'less')        # says no more than before
        self.assertEqual(rel('1878', ''), 'less')
        self.assertEqual(rel('1878-01-12', '1879-01-12'), 'conflict')
        self.assertEqual(rel('1870/1880', '1875/1885'), 'conflict')  # overlap, neither inside

    def test_places(self) -> None:
        rel = gedcom_import._place_relation
        self.assertEqual(rel('Fairview, Kansas', 'fairview,  kansas'), 'eq')
        self.assertEqual(rel('Fairview, Kansas, USA', 'Fairview, Kansas, United States'), 'eq')
        self.assertEqual(rel('', 'Fairview, Kansas'), 'adds')
        self.assertEqual(rel('Fairview, Kansas', ''), 'less')
        self.assertEqual(rel('Springfield, Illinois', 'Springfield, Ohio'), 'conflict')
        # Only the last part is read as a country: a town called USA is not one.
        self.assertEqual(rel('USA, Kansas', 'United States, Kansas'), 'conflict')


class ReimportScaleTestCase(_ArchiveCase):
    def test_thousand_person_update_plan_scans_once_and_mints_nothing(self) -> None:
        import _lib

        def big(name: str, exported: str) -> Path:
            lines = ['0 HEAD', '1 SOUR BigApp', f'1 DATE {exported}', '1 CHAR UTF-8']
            for i in range(1, 1001):
                lines += [f'0 @I{i}@ INDI', f'1 NAME Person{i} /Bulk/',
                          '1 BIRT', '2 DATE 1900']
            lines.append('0 TRLR')
            path = Path(self._tmp.name) / name
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            return path

        self.assertTrue(self._run(big('big.ged', '1 JUL 2026'), apply=True)[0].data['applied'])
        scans = {'ids': 0, 'people': 0, 'sources': 0}
        real_ids, real_people = _lib.scan_ids_in_tree, gedcom_import._scan_existing_persons
        real_sources = gedcom_import._gedcom_source_paths

        def count(key, real):
            def wrapped(*args, **kwargs):
                scans[key] += 1
                return real(*args, **kwargs)
            return wrapped

        with mock.patch.object(_lib, 'scan_ids_in_tree', count('ids', real_ids)), \
                mock.patch.object(gedcom_import, '_scan_existing_persons',
                                  count('people', real_people)), \
                mock.patch.object(gedcom_import, '_gedcom_source_paths',
                                  count('sources', real_sources)):
            result, _out, _err = self._run(big('big-again.ged', '1 JUL 2027'))
        self.assertEqual(result.data['mode'], 'update')
        self.assertEqual(result.data['matched'], 1000)
        self.assertEqual(result.data['same'], 1000)
        self.assertEqual(result.data['claims'], 0)
        # One walk of people/, one of sources/, and no id scan at all.
        self.assertEqual(scans, {'ids': 0, 'people': 1, 'sources': 1})


# ── A newer download: the write (TOOLING §13a2) ──────────────────────────────

def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob('*')) if p.is_file()}


class ReimportApplyTestCase(_ReimportCase):
    """`--apply` on a newer download adds only what changed, and nothing he
    already has is touched. Expectations come from the contract and the two
    fixture files (see `_ReimportCase`), not from a run."""

    def _apply(self, ged: Path = SMALL_V2, **kwargs):
        return self._run(ged, apply=True, **kwargs)

    def _new_record(self) -> Path:
        found = [p for p in (self.archive / 'sources' / 'other').glob('*.md')
                 if p != self.record1]
        self.assertEqual(len(found), 1)
        return found[0]

    def _questions(self) -> str:
        return (self.archive / 'notes' / 'questions.md').read_bytes().decode('utf-8')

    def test_apply_adds_only_what_changed_and_touches_nothing_he_has(self) -> None:
        # He reviewed one claim and wrote in one person's record first.
        import claim as claim_mod
        occupation = self._v1_claim('occupation', 'Thomas Hartley')
        self.assertEqual(claim_mod.run_claim(self.archive, claim_id=occupation['id'],
                                             status='accepted').exit_code, EXIT_CLEAN)
        rose = self._person_path('Rose Hartley')
        rose.write_text(rose.read_text(encoding='utf-8') + '\nShe kept the bible.\n',
                        encoding='utf-8')
        before = _files(self.archive)
        result, out, _err = self._apply()
        self.assertTrue(result.data['applied'])
        self.assertEqual(result.data['mode'], 'update')
        after = _files(self.archive)
        for path, raw in before.items():
            self.assertEqual(after[path], raw, f'{path} was changed')
        added = sorted(set(after) - set(before))
        new_record = self._new_record()
        sid2 = result.data['source_id']
        self.assertEqual(added, sorted([
            f'.cache/gedcom_import/{hashlib.sha256(SMALL_V2.read_bytes()).hexdigest()[:12]}.csv',
            f'documents/gedcom/small-v2_{sid2}.ged',
            f'notes/gedcom-imports/small-v2_{sid2}.csv',
            'notes/questions.md',
            self._person_path('Walter Hartley').relative_to(self.archive).as_posix(),
            new_record.relative_to(self.archive).as_posix(),
        ]))
        self.assertEqual(len(list((self.archive / 'people' / 'stubs').glob('*.md'))), 11)
        record = read_record(new_record)
        self.assertEqual(record['parse_errors'], [])
        self.assertEqual(len(record['claims']), 8)
        self.assertEqual({c['status'] for c in record['claims']}, {'suggested'})
        self.assertEqual(after[f'documents/gedcom/small-v2_{sid2}.ged'], SMALL_V2.read_bytes())
        self.assertIn('Added what changed', out)
        self.assertNotIn('dry-run', out)
        self.assertIn(self.sid1, new_record.read_text(encoding='utf-8'))

    def test_changed_statement_contradicts_the_earlier_one_and_gets_its_question(self) -> None:
        import index as index_mod
        import lint as lint_mod
        result, _out, _err = self._apply()
        old = self._v1_claim('birth', 'Rose Hartley')
        new = [c for c in read_record(self._new_record())['claims']
               if c['type'] == 'birth' and 'Rose Hartley' in c['value']]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]['contradicts'], [old['id']])
        self.assertEqual(str(new[0]['date']), '1879-01-12')
        self.assertIn(self._pid('Rose Hartley'), str(new[0]['persons'][0]))
        blocks = [b for b in self._questions().split('## Q: ') if new[0]['id'] in b]
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertTrue(block.startswith('When and where was Rose Hartley born?'))
        self.assertIn(old['id'], block)
        self.assertIn(self._pid('Rose Hartley'), block)
        self.assertIn('- origin: tool', block)
        self.assertIn('- status: open', block)
        self.assertEqual(self._questions().count('## Q: '), 2)     # and the divorce year
        self.assertEqual(result.data['questions'], 2)
        # Adding detail is not a disagreement: no contradicts, no question.
        ada = [c for c in read_record(self._new_record())['claims']
               if c['type'] == 'birth' and 'Ada Hartley' in c['value']]
        self.assertNotIn('contradicts', ada[0])
        self.assertNotIn(ada[0]['id'], self._questions())
        self.assertEqual(index_mod.build_index(self.archive, self.config).exit_code,
                         EXIT_CLEAN)
        n_errors, _n_warnings, _e018 = lint_mod.run_lint_silent(self.archive, self.config)
        self.assertEqual(n_errors, 0, 'an applied update must lint with no errors (E009 too)')

    def test_questions_are_added_after_the_ones_he_wrote(self) -> None:
        notes = self.archive / 'notes'
        notes.mkdir(exist_ok=True)
        mine = b'# Questions\r\n\r\n## Q: Where was the farm?\r\n- origin: human\r\n- status: open\r\n'
        (notes / 'questions.md').write_bytes(mine)
        self._apply()
        raw = (notes / 'questions.md').read_bytes()
        self.assertTrue(raw.startswith(mine))
        self.assertNotIn(b'\n', raw.replace(b'\r\n', b''))          # his line endings kept
        self.assertEqual(raw.count(b'## Q: '), 3)

    def test_a_failed_write_puts_everything_back(self) -> None:
        notes = self.archive / 'notes'
        notes.mkdir(exist_ok=True)
        (notes / 'questions.md').write_bytes(b'## Q: Where was the farm?\n- status: open\n')
        before = _tree_digest(self.archive)
        real_write = gedcom_import._write_text

        def failing_write(path, text):
            if '.cache' in Path(path).parts:        # the very last write
                raise OSError('disk full (injected)')
            real_write(path, text)

        with mock.patch.object(gedcom_import, '_write_text', failing_write):
            result, _out, err = self._apply()
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('rolled back', err)
        self.assertEqual(_tree_digest(self.archive), before)
        self.assertTrue(self._apply()[0].data['applied'])           # and it can be run again

    def test_a_failed_write_removes_a_questions_file_it_made(self) -> None:
        before = _tree_digest(self.archive)
        real_write = gedcom_import._write_text

        def failing_write(path, text):
            if '.cache' in Path(path).parts:
                raise OSError('disk full (injected)')
            real_write(path, text)

        with mock.patch.object(gedcom_import, '_write_text', failing_write):
            result, _out, _err = self._apply()
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertEqual(_tree_digest(self.archive), before)
        self.assertFalse((self.archive / 'notes' / 'questions.md').exists())

    def test_a_questions_file_that_cannot_be_read_stops_before_any_write(self) -> None:
        notes = self.archive / 'notes'
        notes.mkdir(exist_ok=True)
        (notes / 'questions.md').write_bytes(b'## Q: caf\xe9?\n')   # not UTF-8
        before = _tree_digest(self.archive)
        result, _out, err = self._apply()
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('notes/questions.md', err)
        self.assertIn('UTF-8', err)
        self.assertIn('Nothing was written', err)
        self.assertEqual(_tree_digest(self.archive), before)

    def test_nothing_new_writes_nothing(self) -> None:
        before = _tree_digest(self.archive)
        result, out, _err = self._apply(self._same_tree_redownloaded())
        self.assertEqual(result.exit_code, EXIT_WARNINGS)           # small.ged's own warnings
        self.assertFalse(result.data['applied'])
        self.assertEqual(result.changed, [])
        self.assertIn('Nothing new', out)
        self.assertIn('Nothing was written', out)
        self.assertEqual(_tree_digest(self.archive), before)

    def test_the_same_newer_file_twice_is_refused(self) -> None:
        self._apply()
        before = _tree_digest(self.archive)
        result, _out, err = self._apply()
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('already imported', err)
        self.assertEqual(_tree_digest(self.archive), before)

    def test_a_third_download_builds_on_the_first_two(self) -> None:
        sid2 = self._apply()[0].data['source_id']
        record2 = self._new_record()
        v1 = self._v1_claim('birth', 'Rose Hartley')
        v2 = next(c for c in read_record(record2)['claims']
                  if c['type'] == 'birth' and 'Rose Hartley' in c['value'])
        third = self._edited('small-v3.ged', SMALL_V2,
                             ('2 DATE 12 JAN 1879', '2 DATE 12 JAN 1880'),
                             ('1 DATE 1 JUL 2027', '1 DATE 1 JUL 2028'))
        plan = self._plan(third)
        self.assertEqual(plan.mode, 'update')
        self.assertEqual(plan.stubs, [])                            # Walter is known now
        self.assertEqual(plan.held, {})
        self.assertEqual([f.outcome for f in plan.facts if f.outcome != 'same'], ['changed'])
        result, _out, _err = self._apply(third)
        self.assertTrue(result.data['applied'])
        self.assertEqual(sorted(result.data['update_of']), sorted([self.sid1, sid2]))
        record3 = next(p for p in (self.archive / 'sources' / 'other').glob('*.md')
                       if p not in (self.record1, record2))
        claims = read_record(record3)['claims']
        self.assertEqual(len(claims), 1)
        self.assertEqual(sorted(claims[0]['contradicts']), sorted([v1['id'], v2['id']]))
        self.assertEqual(self._questions().count(claims[0]['id']), 1)
        self.assertEqual(len(list((self.archive / 'people' / 'stubs').glob('*.md'))), 11)
        listing = next((self.archive / 'notes' / 'gedcom-imports').glob(
            f'*{result.data["source_id"]}.csv')).read_text(encoding='utf-8')
        self.assertRegex(listing, r'(?m)^# update_of: (\S+), (\S+)$')
        self.assertIn(self.sid1, listing)
        self.assertIn(sid2, listing)

    def test_people_not_matched_for_certain_get_nothing_written(self) -> None:
        # @I4@ was Ben Hartley and is Wilbur Hartley, born 1897, in this file.
        reused = self._edited(
            'reused.ged', SMALL_V2,
            ('1 NAME Ben /Hartley/\n1 SEX M\n1 BIRT\n2 DATE INT 1902 (spring after the flood)\n',
             '1 NAME Wilbur /Hartley/\n1 SEX M\n1 BIRT\n2 DATE 1897\n'))
        ben = self._person_path('Ben Hartley')
        ben_before = ben.read_bytes()
        result, out, _err = self._apply(reused)
        self.assertTrue(result.data['applied'])
        self.assertEqual(len(result.data['unmatched']), 1)
        self.assertIn('Not matched for certain', out)
        self.assertEqual(ben.read_bytes(), ben_before)
        text = self._new_record().read_text(encoding='utf-8')
        self.assertNotIn('Wilbur', text)
        self.assertNotIn(self._pid('Ben Hartley'), text)
        names = [read_record(p)['meta']['name']
                 for p in (self.archive / 'people' / 'stubs').glob('*.md')]
        self.assertNotIn('Wilbur Hartley', names)

    def test_working_copy_refuses_the_update_too(self) -> None:
        (self.archive / 'WORKING_COPY').write_text('no assets here\n', encoding='utf-8')
        before = _tree_digest(self.archive)
        result, _out, _err = self._apply()
        self.assertEqual(result.data['status'], 'working-copy')
        self.assertEqual(_tree_digest(self.archive), before)

    def test_unreachable_documents_drive_refuses_the_update(self) -> None:
        before = _tree_digest(self.archive)
        with mock.patch.object(gedcom_import, '_unreachable_anchor', return_value='Q:\\'):
            result, _out, err = self._apply()
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertIn('roots:', err)
        self.assertEqual(_tree_digest(self.archive), before)

    def test_three_id_scans_at_most_and_none_for_a_plan(self) -> None:
        import _lib
        scans = {'n': 0}
        real = _lib.scan_ids_in_tree

        def counted(*args, **kwargs):
            scans['n'] += 1
            return real(*args, **kwargs)

        with mock.patch.object(_lib, 'scan_ids_in_tree', counted):
            self._run(SMALL_V2)
            self.assertEqual(scans['n'], 0)
            self._apply()
        self.assertEqual(scans['n'], 3)                              # source, people, claims

    def test_obsidian_links_name_the_files_people_already_have(self) -> None:
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\nlinks: obsidian\n',
            encoding='utf-8')
        self.config = load_fha_yaml(self.archive, strict=True)
        self._apply()
        text = self._new_record().read_text(encoding='utf-8')
        notes = text[text.index('## Notes'):]
        self.assertIn(f'[[{self.record1.stem}|', notes)


class ImportListTestCase(_ReimportCase):
    """The record-number list is kept with the records (`notes/gedcom-imports/`),
    so it is backed up and survives a lost `.cache`."""

    def _list(self, sid: str) -> str:
        return next((self.archive / 'notes' / 'gedcom-imports').glob(
            f'*_{sid}.csv')).read_bytes().decode('utf-8')

    def _rows(self, sid: str) -> list[dict]:
        import csv
        body = [ln for ln in self._list(sid).splitlines() if not ln.startswith('#')]
        return list(csv.DictReader(body))

    def test_a_first_import_writes_the_list_beside_the_records(self) -> None:
        text = self._list(self.sid1)
        self.assertIn('# format: 2', text)
        self.assertIn(f'# source_id: {self.sid1}', text)
        people = [r for r in self._rows(self.sid1) if r['kind'] == 'person']
        self.assertEqual(len(people), 10)
        self.assertEqual({r['origin'] for r in people}, {'new'})
        rose = next(r for r in people if r['gedcom_xref'] == '@I1@')
        self.assertEqual(rose['minted_id'], self._pid('Rose Hartley'))
        # The cache copy is still the last write and says the same.
        cache = next((self.archive / '.cache' / 'gedcom_import').glob('*.csv'))
        self.assertEqual(cache.read_bytes().decode('utf-8'), text)

    def test_an_update_lists_every_person_and_how_they_were_matched(self) -> None:
        result, _out, _err = self._run(SMALL_V2, apply=True)
        sid2 = result.data['source_id']
        self.assertIn(f'# update_of: {self.sid1}', self._list(sid2))
        people = {r['gedcom_xref']: r for r in self._rows(sid2) if r['kind'] == 'person'}
        self.assertEqual(len(people), 11)
        self.assertEqual(people['@I1@']['origin'], 'record number')
        self.assertEqual(people['@I1@']['minted_id'], self._pid('Rose Hartley'))
        self.assertEqual(people['@I11@']['origin'], 'new')
        self.assertEqual(people['@I11@']['minted_id'], self._pid('Walter Hartley'))

    def test_an_older_list_without_the_new_columns_is_still_read(self) -> None:
        import csv
        shutil.rmtree(self.archive / 'notes' / 'gedcom-imports')
        cache = next((self.archive / '.cache' / 'gedcom_import').glob('*.csv'))
        old: list[str] = []
        for line in cache.read_text(encoding='utf-8').splitlines():
            if line.startswith(('# format', '# update_of')):
                continue
            if line.startswith('#'):
                old.append(line)
            else:
                buf = io.StringIO()
                csv.writer(buf, lineterminator='').writerow(next(csv.reader([line]))[:4])
                old.append(buf.getvalue())
        cache.write_text('\n'.join(old) + '\n', encoding='utf-8')
        self.assertNotIn('origin', cache.read_text(encoding='utf-8'))
        plan = self._plan(SMALL_V2)
        self.assertEqual(len(plan.matches), 10)
        self.assertEqual(plan.held, {})

    def test_a_person_with_a_name_and_nothing_else_is_placed_without_the_cache(self) -> None:
        # Karl has no statement of his own, so no claim says which record he
        # became. Only the list knows, and the list is not in `.cache`.
        lines = ['0 HEAD', '1 SOUR OtherApp', '1 CHAR UTF-8',
                 '0 @I1@ INDI', '1 NAME Ada /Thorsson/', '1 SEX F', '1 BIRT', '2 DATE 1901',
                 '0 @I2@ INDI', '1 NAME Karl /Thorsson/', '1 SEX M', '0 TRLR', '']
        first = self._ged('other.ged', '\n'.join(lines))
        self.assertTrue(self._run(first, apply=True)[0].data['applied'])
        later = self._ged('other-later.ged', '\n'.join(
            lines[:-2] + ['0 @I3@ INDI', '1 NAME Nils /Thorsson/', '1 SEX M', '0 TRLR', '']))
        shutil.rmtree(self.archive / '.cache')
        plan = self._plan(later)
        self.assertEqual(sorted(plan.matches), ['@I1@', '@I2@'])
        self.assertEqual(plan.matches['@I2@'].pid, self._pid('Karl Thorsson'))
        self.assertEqual([s.name for s in plan.stubs], ['Nils Thorsson'])


class ConfirmLikelyMatchesTestCase(_ReimportCase):
    """A tree whose program gave everyone a new number: nobody is matched for
    certain, each person is listed with the one they most likely are, and one
    yes to that list places them."""

    def setUp(self) -> None:
        super().setUp()
        self.renumbered = self._ged('renumbered.ged', self._renumbered(_lf(SMALL_V2)))

    def test_without_his_yes_nothing_is_written_for_them(self) -> None:
        result, out, _err = self._run(self.renumbered, apply=True)
        self.assertEqual(result.data['matched'], 0)
        self.assertEqual(len(result.data['unmatched']), 10)
        self.assertIn('--confirm-matches', out)
        record = next(p for p in (self.archive / 'sources' / 'other').glob('*.md')
                      if p != self.record1)
        # Only Walter, who is new, and his own birth.
        self.assertEqual([c['type'] for c in read_record(record)['claims']], ['birth'])

    def test_the_list_is_shown_and_no_is_no(self) -> None:
        asked: list[str] = []

        def no(prompt: str) -> bool:
            asked.append(prompt)
            return False

        before = _tree_digest(self.archive)
        result, out, _err = self._run(self.renumbered, confirm_matches=True, confirm=no)
        self.assertEqual(len(asked), 1)
        self.assertIn('Rose Hartley', out)
        self.assertIn(self._pid('Rose Hartley'), out)
        self.assertEqual(result.data['matched'], 0)
        self.assertEqual(result.data['confirmed'], 0)
        self.assertEqual(_tree_digest(self.archive), before)

    def test_his_yes_places_them_and_the_update_is_planned(self) -> None:
        before = _tree_digest(self.archive)
        result, _out, _err = self._run(self.renumbered, confirm_matches=True,
                                       confirm=lambda prompt: True)
        self.assertEqual(_tree_digest(self.archive), before)        # a plan still writes nothing
        self.assertEqual(result.data['confirmed'], 10)
        self.assertEqual(result.data['matched'], 10)
        self.assertEqual(result.data['unmatched'], [])
        self.assertEqual(result.data['new_people'], 1)
        self.assertEqual(result.data['changed'], 2)

    def test_his_yes_then_apply_writes_the_update_and_remembers_the_numbers(self) -> None:
        result, _out, _err = self._run(self.renumbered, apply=True, confirm_matches=True,
                                       confirm=lambda prompt: True)
        self.assertTrue(result.data['applied'])
        self.assertEqual(len(list((self.archive / 'people' / 'stubs').glob('*.md'))), 11)
        listing = next((self.archive / 'notes' / 'gedcom-imports').glob(
            f'*_{result.data["source_id"]}.csv')).read_bytes().decode('utf-8')
        self.assertIn(f'@P101@,{self._pid("Rose Hartley")},person,Rose Hartley,you confirmed',
                      listing)
        # Next year's download keeps the new numbers: matched with no question asked.
        third = self._ged('renumbered-again.ged', self._renumbered(_lf(SMALL_V2)).replace(
            '1 DATE 1 JUL 2027', '1 DATE 1 JUL 2028'))
        plan = self._plan(third)
        self.assertEqual(len(plan.matches), 11)
        self.assertEqual(plan.held, {})
        self.assertEqual(plan.claims, [])

    def test_no_answer_is_no(self) -> None:
        with mock.patch('builtins.input', side_effect=EOFError):
            result, _out, _err = self._run(self.renumbered, confirm_matches=True)
        self.assertEqual(result.data['confirmed'], 0)

    def test_the_flag_reaches_the_command_line(self) -> None:
        with mock.patch('builtins.input', return_value='y'), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = gedcom_import._standalone_main(
                [str(self.renumbered), '--confirm-matches', '--apply',
                 '--root', str(self.archive)])
        self.assertEqual(code, EXIT_WARNINGS)
        self.assertEqual(len(list((self.archive / 'people' / 'stubs').glob('*.md'))), 11)


class ReimportReviewFixesTestCase(_ReimportCase):
    """What a later download must not forget or bring back: a match he
    confirmed earlier the same day, a yes that could not be kept, and a
    statement he deleted while the earlier download's copy is missing."""

    @staticmethod
    def _sisters(*people: tuple[str, ...]) -> str:
        lines = ['0 HEAD', '1 SOUR OtherApp', '1 CHAR UTF-8']
        for xref, name, born, *more in people:
            lines += [f'0 {xref} INDI', f'1 NAME {name}', '1 SEX F',
                      '1 BIRT', f'2 DATE {born}', *more]
        return '\n'.join(lines + ['0 TRLR', ''])

    ANNA = ('Anna /Berg/', '1900', '1 OCCU Seamstress')
    CLARA = ('Clara /Berg/', '1905', '1 OCCU Teacher')

    def _with_sids(self, *sids: str):
        """Mint these source ids, in order, so which one sorts first is fixed."""
        import _lib
        queue = list(sids)
        real = _lib._mint_candidate

        def candidate(prefix: str) -> str:
            return queue.pop(0) if prefix.upper() == 'S' and queue else real(prefix)
        return mock.patch.object(_lib, '_mint_candidate', side_effect=candidate)

    def test_a_match_he_confirmed_is_remembered_on_a_same_day_download(self) -> None:
        # The first download's id sorts after the second's: the order must
        # come from which updated which, not from the ids.
        with self._with_sids('s-zzzzzzzzz2', 's-0000000002'):
            self._run(self._ged('berg.ged', self._sisters(
                ('@I1@',) + self.ANNA, ('@I2@',) + self.CLARA)), apply=True)
            result, _out, _err = self._run(
                self._ged('berg-v2.ged', self._sisters(
                    ('@I1@',) + self.CLARA,
                    ('@I2@',) + self.ANNA + ('1 RESI', '2 PLAC Topeka, Kansas'))),
                apply=True, confirm_matches=True, confirm=lambda prompt: True)
        self.assertEqual(result.data['confirmed'], 2)
        self.assertEqual(result.data['source_id'], 's-0000000002')
        plan = self._plan(self._ged('berg-v3.ged', self._sisters(
            ('@I1@',) + self.CLARA + ('1 RESI', '2 PLAC Wichita, Kansas'),
            ('@I2@',) + self.ANNA + ('1 RESI', '2 PLAC Topeka, Kansas'))))
        self.assertEqual(plan.held, {})
        self.assertEqual(plan.prior[0].sid, 's-0000000002')    # the newest first
        self.assertEqual(plan.matches['@I1@'].pid, self._pid('Clara Berg'))
        self.assertEqual([c.value for c in plan.claims],
                         ['Clara Berg: residence, Wichita, Kansas (GEDCOM @I1@)'])

    def test_a_yes_that_cannot_be_kept_says_so(self) -> None:
        self._run(self._ged('berg.ged', self._sisters(
            ('@I1@',) + self.ANNA, ('@I2@',) + self.CLARA)), apply=True)
        before = _tree_digest(self.archive)
        result, out, _err = self._run(
            self._ged('berg-v2.ged', self._sisters(
                ('@I1@',) + self.CLARA, ('@I2@',) + self.ANNA)),
            apply=True, confirm_matches=True, confirm=lambda prompt: True)
        self.assertEqual(result.data['confirmed'], 2)
        self.assertFalse(result.data['applied'])
        self.assertEqual(_tree_digest(self.archive), before)
        self.assertIn('the next download will ask again', out)
        self.assertNotIn('--apply', out)

    def test_nothing_new_never_tells_him_to_apply(self) -> None:
        for apply in (False, True):
            _result, out, _err = self._run(self._same_tree_redownloaded(), apply=apply)
            self.assertIn('Nothing new', out)
            self.assertNotIn('--apply', out)
            self.assertNotIn('Source record:', out)

    def test_a_deleted_statement_is_not_brought_back_without_the_earlier_copy(self) -> None:
        text = self.record1.read_text(encoding='utf-8')
        start = text.index("- value: 'Thomas Hartley: occupation Bookkeeper")
        end = text.index('- value:', start + 1)
        self.record1.write_text(text[:start] + text[end:], encoding='utf-8')
        self.filed1.rename(self.filed1.with_suffix('.moved'))
        before = _tree_digest(self.archive)
        result, out, err = self._run(SMALL_V2, apply=True)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIn('restore it from your backup', err)
        self.assertEqual(_tree_digest(self.archive), before)
        # The plan alone still runs, and says why it cannot be applied.
        result, out, _err = self._run(SMALL_V2)
        self.assertIn('Nothing can be added until that copy is back', out)


class OwnerDecisionsTestCase(_ArchiveCase):
    """The owner's answers to the plan's open questions."""

    _tree = ReimportMatchingTestCase._tree
    _plan = ReimportMatchingTestCase._plan

    def _first(self, *people) -> None:
        self.assertTrue(self._run(self._tree('first.ged', list(people)),
                                  apply=True)[0].data['applied'])

    def test_same_number_and_part_of_the_name_with_no_birth_date_is_a_match(self) -> None:
        self._first(('@I1@', 'Thomas /Hartley/', 'M', '', '1 OCCU Farmer'),
                    ('@I2@', 'Rose /Hartley/', 'F', '1878'))
        later = self._tree('later.ged', [
            ('@I1@', 'Thomas Edward /Hartley/', 'M', '', '1 OCCU Farmer'),
            ('@I2@', 'Rose /Hartley/', 'F', '1878')])
        plan = self._plan(later)
        self.assertEqual(sorted(plan.matches), ['@I1@', '@I2@'])
        self.assertEqual(plan.held, {})
        self.assertEqual(plan.stubs, [])
        self.assertEqual(plan.claims, [])
        # He is told the tree spells the name differently; his record keeps its name.
        self.assertTrue(any('Thomas Edward Hartley' in line for line in plan.name_changes))

    def test_a_son_moved_onto_his_undated_fathers_number_is_still_held(self) -> None:
        # Father and son, neither dated. The father was deleted from the tree
        # and the program closed the gap, so the son now carries his number.
        self._first(('@I1@', 'Thomas /Hartley/', 'M', '', '1 OCCU Farmer'),
                    ('@I2@', 'Thomas Edward /Hartley/', 'M', '', '1 OCCU Blacksmith'),
                    ('@I3@', 'Rose /Hartley/', 'F', '1878'))
        later = self._tree('later.ged', [
            ('@I1@', 'Thomas Edward /Hartley/', 'M', '', '1 OCCU Blacksmith'),
            ('@I2@', 'Rose /Hartley/', 'F', '1878')])
        plan = self._plan(later)
        self.assertNotIn('@I1@', plan.matches)
        self.assertIn('@I1@', plan.held)
        self.assertEqual(plan.claims, [])

    def test_part_of_the_name_and_a_birth_date_on_one_side_only_is_still_held(self) -> None:
        self._first(('@I1@', 'Thomas /Hartley/', 'M', '', '1 OCCU Farmer'),
                    ('@I2@', 'Rose /Hartley/', 'F', '1878'))
        later = self._tree('later.ged', [
            ('@I1@', 'Thomas Edward /Hartley/', 'M', '1850', '1 OCCU Farmer'),
            ('@I2@', 'Rose /Hartley/', 'F', '1878')])
        self.assertIn('@I1@', self._plan(later).held)

    def test_a_wholly_different_undated_name_is_still_held(self) -> None:
        self._first(('@I1@', 'Thomas /Hartley/', 'M', '', '1 OCCU Farmer'),
                    ('@I2@', 'Rose /Hartley/', 'F', '1878'))
        later = self._tree('later.ged', [('@I1@', 'Silas /Cole/', 'M', '', '1 OCCU Farmer'),
                                         ('@I2@', 'Rose /Hartley/', 'F', '1878')])
        self.assertIn('@I1@', self._plan(later).held)

    def test_a_place_that_only_gains_parts_is_more_detail_with_no_question(self) -> None:
        self._first(('@I1@', 'Rose /Hartley/', 'F', '', '1 BIRT', '2 DATE 12 JAN 1878',
                     '2 PLAC Springfield, Illinois'))
        later = self._tree('later.ged', [
            ('@I1@', 'Rose /Hartley/', 'F', '', '1 BIRT', '2 DATE 12 JAN 1878',
             '2 PLAC Springfield, Sangamon County, Illinois')])
        plan = self._plan(later)
        self.assertEqual([f.outcome for f in plan.facts], ['more detail'])
        self.assertEqual(plan.questions, [])
        result, _out, _err = self._run(later, apply=True)
        self.assertTrue(result.data['applied'])
        self.assertFalse((self.archive / 'notes' / 'questions.md').exists())
        record = next((self.archive / 'sources' / 'other').glob('later_*.md'))
        claim = read_record(record)['claims'][0]
        self.assertNotIn('contradicts', claim)
        self.assertEqual(claim['place_text'], 'Springfield, Sangamon County, Illinois')

    def test_place_parts(self) -> None:
        rel = gedcom_import._place_relation
        self.assertEqual(rel('Springfield, Illinois', 'Springfield, Sangamon County, Illinois'),
                         'adds')
        self.assertEqual(rel('Springfield, Illinois', 'Springfield, Illinois, USA'), 'adds')
        self.assertEqual(rel('Springfield, Sangamon County, Illinois', 'Springfield, Illinois'),
                         'less')
        # A part that changed is still a disagreement, whatever else was added.
        self.assertEqual(rel('Springfield, Illinois', 'Springfield, Greene County, Missouri'),
                         'conflict')
        self.assertEqual(rel('Springfield, Illinois', 'Illinois, Springfield'), 'conflict')


# ── Dates that cannot all be true, listed in the plan (TOOLING §13a2) ─────────

def _family_ged(pairs: list[tuple[str, str]]) -> str:
    """A tree of mothers and one child each: (mother's death, child's birth)."""
    lines = ['0 HEAD', '1 SOUR AncestryTree', '1 GEDC', '2 VERS 5.5.1', '1 CHAR UTF-8']
    for n, (died, born) in enumerate(pairs, start=1):
        lines += [f'0 @M{n}@ INDI', f'1 NAME Ann{n} /Cole/', '1 SEX F',
                  '1 BIRT', '2 DATE 1840', '1 DEAT', f'2 DATE {died}', f'1 FAMS @F{n}@',
                  f'0 @C{n}@ INDI', f'1 NAME Margaret{n} /Cole/', '1 SEX F',
                  '1 BIRT', f'2 DATE {born}', f'1 FAMC @F{n}@',
                  f'0 @F{n}@ FAM', f'1 WIFE @M{n}@', f'1 CHIL @C{n}@']
    return '\n'.join(lines + ['0 TRLR']) + '\n'


class PlausibilityPlanTestCase(_ArchiveCase):
    """He reads the plan before anything is written. A tree where Margaret
    Cole is born two years after her mother died should say so there, in
    plain words with the file's line numbers, and change nothing else: the
    claims are imported as they are, for him to judge when he reviews."""

    def _ged(self, name: str, pairs: list[tuple[str, str]]) -> Path:
        path = Path(self._tmp.name) / name
        path.write_text(_family_ged(pairs), encoding='utf-8')
        return path

    def test_plan_lists_a_child_born_after_mother_died(self) -> None:
        ged = self._ged('cole.ged', [('1869', '1871')])
        before = _tree_digest(self.archive)
        result, out, _err = self._run(ged)
        self.assertEqual(_tree_digest(self.archive), before)       # writes nothing
        self.assertEqual(result.exit_code, EXIT_CLEAN)             # not a warning
        self.assertIn('Dates that cannot all be true (1)', out)
        [message] = result.data['plausibility']
        self.assertIn('Margaret1 Cole (@C1@)', message)
        self.assertIn('mother Ann1 Cole (@M1@)', message)
        # Each date and the mother tie point at the line in the file
        # (GEDCOM lines count from 1; the event is the line above its DATE).
        text = ged.read_text(encoding='utf-8').splitlines()
        birth_line = text.index('2 DATE 1871')
        death_line = text.index('2 DATE 1869')
        fam_line = text.index('0 @F1@ FAM') + 1
        self.assertIn(f'1871 (line {birth_line})', message)
        self.assertIn(f'1869 (line {death_line})', message)
        self.assertIn(f'comes from line {fam_line}', message)
        self.assertIn(message, out)

    def test_no_conflict_no_section(self) -> None:
        result, out, _err = self._run(self._ged('cole.ged', [('1869', '1866')]))
        self.assertEqual(result.data['plausibility'], [])
        self.assertNotIn('cannot all be true', out)

    def test_small_tree_plan_has_no_section(self) -> None:
        # The shipped fixture has no impossible dates: its plan is unchanged.
        result, out, _err = self._run(SMALL)
        self.assertEqual(result.data['plausibility'], [])
        self.assertNotIn('cannot all be true', out)

    def test_imported_claims_are_judged_though_still_suggested(self) -> None:
        # Every claim an import writes is `suggested`, which lint does not
        # read as a fact yet; the plan must judge them anyway.
        seen = []
        real = gedcom_import.plausibility_input_from_claims

        def spy(*args, **kwargs):
            seen.append(kwargs.get('statuses'))
            return real(*args, **kwargs)

        with mock.patch.object(gedcom_import, 'plausibility_input_from_claims', spy):
            result, _out, _err = self._run(self._ged('cole.ged', [('1869', '1871')]))
        self.assertEqual(seen, [('suggested',)])
        self.assertEqual(len(result.data['plausibility']), 1)

    def test_plan_out_lists_every_problem(self) -> None:
        n = gedcom_import._PLAN_DETAIL_CAP + 5
        ged = self._ged('many.ged', [('1869', '1871')] * n)
        out_file = Path(self._tmp.name) / 'plan.txt'
        result, out, _err = self._run(ged, plan_out=str(out_file))
        self.assertEqual(len(result.data['plausibility']), n)
        self.assertIn(f'Dates that cannot all be true ({n})', out)
        self.assertIn('... and 5 more', out)
        self.assertNotIn(f'Margaret{n} Cole', out)
        full = out_file.read_text(encoding='utf-8')
        for i in range(1, n + 1):
            self.assertIn(f'Margaret{i} Cole (@C{i}@) is recorded as born', full)

    def test_plan_problems_do_not_change_what_is_written(self) -> None:
        # The same file applied with and without the check writes the same
        # bytes: the plan only reports. Ids are minted in order so the two
        # archives can be compared file for file.
        def deterministic(prefix, count, _root):
            return [f'{prefix}-{i:010d}' for i in range(1, count + 1)]

        digests = []
        for with_check in (True, False):
            self.tearDown()
            self.setUp()
            ged = self._ged('cole.ged', [('1869', '1871')])
            patches = [mock.patch.object(gedcom_import, 'mint_ids', deterministic),
                       mock.patch.object(gedcom_import, '_today', return_value='2026-11-24')]
            if not with_check:
                patches.append(mock.patch.object(
                    gedcom_import, '_plan_plausibility', return_value=[]))
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                result, _out, _err = self._run(ged, apply=True)
            self.assertTrue(result.data['applied'])
            self.assertEqual(len(result.data['plausibility']), 1 if with_check else 0)
            digests.append(_tree_digest(self.archive))
        self.assertEqual(digests[0], digests[1])


class PlausibilityReimportPlanTestCase(_ReimportCase):
    """A newer download is judged as a whole tree: a new child of a mother
    already in the archive is read against the death the newer file gives
    her, so the plan says so before anything is added."""

    def test_new_child_born_after_matched_mother_died(self) -> None:
        ged = self._edited('small-v2-late.ged', SMALL_V2, ('2 DATE 1899\n', '2 DATE 1955\n'))
        before = _tree_digest(self.archive)
        result, out, _err = self._run(ged)
        self.assertEqual(_tree_digest(self.archive), before)
        self.assertEqual(result.data['mode'], 'update')
        born_after = [m for m in result.data['plausibility'] if 'but his mother' in m]
        self.assertEqual(len(born_after), 1, result.data['plausibility'])
        self.assertIn('Walter Hartley (@I11@)', born_after[0])
        self.assertIn('mother Rose Hartley', born_after[0])
        self.assertIn('Dates that cannot all be true', out)

    def test_a_death_he_rejected_is_not_listed_again(self) -> None:
        # He rejected Rose's 1950 death last year. The newer download still
        # says it, beside a son born after it; the plan imports nothing of
        # that death, so it must not list it as a conflict to review.
        self._settled_death_is_not_listed('rejected')

    def test_a_death_he_disputed_is_not_listed_again(self) -> None:
        # Disputing is what the plan's own message suggests; once he has, the
        # next download must not raise the same conflict again (fha lint
        # stops naming a disputed claim too).
        self._settled_death_is_not_listed('disputed')

    def _settled_death_is_not_listed(self, status: str) -> None:
        import claim as claim_mod
        cid = self._v1_claim('death', 'Rose Hartley')['id']
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rr = claim_mod.run_claim(self.archive, claim_id=cid, status=status,
                                     notes='the 1950 death is another Rose')
        self.assertEqual(rr.exit_code, EXIT_CLEAN)
        ged = self._edited('small-v2-late.ged', SMALL_V2, ('2 DATE 1899\n', '2 DATE 1955\n'))
        result, _out, _err = self._run(ged)
        self.assertEqual(result.data['mode'], 'update')
        self.assertEqual([m for m in result.data['plausibility'] if 'but his mother' in m], [])

    def test_newer_download_without_conflicts_has_no_section(self) -> None:
        result, out, _err = self._run(SMALL_V2)
        self.assertEqual(result.data['plausibility'], [])
        self.assertNotIn('cannot all be true', out)


if __name__ == '__main__':
    unittest.main()
