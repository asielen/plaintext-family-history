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


if __name__ == '__main__':
    unittest.main()
