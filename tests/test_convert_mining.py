"""Tests for `fha convert-mining` (BUILD.md M7.5 - legacy interview migration).

Copies the tests/fixtures/legacy-export/ input to a throwaway tree, exercises
the dry-run (writes nothing) and `--apply` (mints sources/claims/person stubs,
imports stories + questions, writes the mapping), and asserts the converted
archive lints with no errors - the M7.5 "Done when" contract.

Run: python -m unittest tests.test_convert_mining -v   (from the repo root)
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import convert_mining
import lint
from _lib import EXIT_CLEAN, EXIT_ERRORS, EXIT_WARNINGS, load_fha_yaml, read_record

FIXTURE = ROOT / 'tests' / 'fixtures' / 'legacy-export'


class ConvertMiningTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive = Path(self._tmp.name) / 'legacy-export'
        shutil.copytree(FIXTURE, self.archive)
        self.config = load_fha_yaml(self.archive, strict=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _apply(self) -> int:
        return convert_mining.run_convert(self.archive, self.config, apply=True)

    # ── dry-run ──────────────────────────────────────────────────────────────

    def test_dry_run_writes_nothing(self) -> None:
        rc = convert_mining.run_convert(self.archive, self.config, apply=False)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse((self.archive / 'sources').exists())
        self.assertFalse((self.archive / 'people').exists())
        self.assertFalse((self.archive / '.cache' / 'convert_mapping.csv').exists())

    # ── apply ────────────────────────────────────────────────────────────────

    def test_apply_creates_sources_claims_and_stubs(self) -> None:
        self.assertEqual(self._apply(), EXIT_CLEAN)

        sources = sorted((self.archive / 'sources' / 'interview').glob('*_S-*.md'))
        self.assertEqual(len(sources), 2)
        stubs = sorted((self.archive / 'people' / 'stubs').glob('*_P-*.md'))
        self.assertEqual(len(stubs), 2)
        stub_meta = read_record(stubs[0])['meta']
        self.assertEqual(stub_meta['tier'], 'stub')
        self.assertEqual(stub_meta['living'], 'unknown')

        mary = next(p for p in sources if 'mary' in p.name)
        rec = read_record(mary)
        self.assertEqual(rec['parse_errors'], [])
        self.assertEqual(rec['meta']['source_type'], 'interview')

        # Transcript filed under documents/interviews/ with the S-id, name kept.
        transcripts = list((self.archive / 'documents' / 'interviews').glob('*_S-*.txt'))
        self.assertEqual(len(transcripts), 2)
        self.assertEqual(rec['meta']['files'][0]['original_filename'], 'T001.txt')

        claims = rec['claims']
        by_type = {c['type']: c for c in claims}
        self.assertIn('birth', by_type)
        self.assertIn('marriage', by_type)
        for c in claims:
            self.assertEqual(c['status'], 'suggested')
            self.assertTrue(c['id'].startswith('C-'))
        # Earliest==Latest collapses to a single EDTF value.
        self.assertEqual(by_type['birth']['date'], '1890')
        # Update(T###) line merged into the claim notes.
        self.assertIn('April', by_type['marriage']['notes'])
        # Best-effort anchor resolved to the transcript line.
        self.assertEqual(by_type['birth']['anchor'], 'line 3')
        text = mary.read_text(encoding='utf-8')
        self.assertIn('## AI Passes', text)
        self.assertIn('model: gpt-4-class', text)
        self.assertIn('human_reviewed: false', text)

    def test_apply_imports_stories_and_questions(self) -> None:
        self._apply()
        mary = next((self.archive / 'sources' / 'interview').glob('*mary*_S-*.md'))
        body = mary.read_text(encoding='utf-8')
        self.assertIn('## Stories', body)
        self.assertIn('wagon journey', body)
        self.assertIn('[P-a1a1a1a1a1]', body)            # person resolved to a token

        questions = (self.archive / 'notes' / 'questions.md').read_text(encoding='utf-8')
        self.assertIn('## Q: Where exactly was Mary Hartley born?', questions)
        self.assertIn('origin: tool', questions)
        self.assertIn('S-', questions)                   # source ref mapped to its S-id

    def test_blank_claim_cell_skipped_with_warning(self) -> None:
        facts = self.archive / 'mining' / 'facts.txt'
        text = facts.read_text(encoding='utf-8')
        text += (
            '\n| Mary Hartley |  | 1900 | 1900 | High | Vitals | |\n'
        )
        facts.write_text(text, encoding='utf-8')

        plan = convert_mining.build_plan(self.archive, self.config, self.archive / 'mining')
        self.assertTrue(any('blank' in w.lower() and 'Claim' in w for w in plan.warnings))
        mary_source = next(s for s in plan.sources if s.legacy_id == 'S001')
        self.assertTrue(all(c.value for c in mary_source.claims))

    def test_unattached_story_warns(self) -> None:
        stories = self.archive / 'mining' / 'stories.txt'
        text = stories.read_text(encoding='utf-8')
        text += '\n## Someone Else (S999)\nAn orphaned story with no matching source.\n'
        stories.write_text(text, encoding='utf-8')

        plan = convert_mining.build_plan(self.archive, self.config, self.archive / 'mining')
        self.assertTrue(any('S999' in w and 'story' in w.lower() for w in plan.warnings))

    def test_question_with_unknown_source_warns(self) -> None:
        questions = self.archive / 'mining' / 'questions.txt'
        text = questions.read_text(encoding='utf-8')
        text += '\n## Q: What happened to the missing source?\nsource: S999\n'
        questions.write_text(text, encoding='utf-8')

        plan = convert_mining.build_plan(self.archive, self.config, self.archive / 'mining')
        self.assertTrue(any('S999' in w and 'questions.txt' in w for w in plan.warnings))
        new_q = next(q for q in plan.questions
                     if q['question'] == 'What happened to the missing source?')
        self.assertEqual(new_q['refs'], [])

    def test_apply_writes_mapping_csv(self) -> None:
        self._apply()
        mapping = (self.archive / '.cache' / 'convert_mapping.csv').read_text(encoding='utf-8')
        self.assertIn('legacy_id,new_id,notes', mapping)
        self.assertIn('S001,S-', mapping)
        self.assertIn('Mary Hartley,P-a1a1a1a1a1', mapping)

    def test_apply_refuses_existing_mapping(self) -> None:
        cache = self.archive / '.cache'
        cache.mkdir()
        (cache / 'convert_mapping.csv').write_text('already converted\n', encoding='utf-8')

        rc = self._apply()
        self.assertEqual(rc, EXIT_ERRORS)
        self.assertFalse((self.archive / 'sources').exists())
        self.assertEqual(
            (cache / 'convert_mapping.csv').read_text(encoding='utf-8'),
            'already converted\n',
        )

    def test_apply_rolls_back_after_write_failure(self) -> None:
        with mock.patch('convert_mining.shutil.copy2', side_effect=OSError('disk full')):
            rc = self._apply()

        self.assertEqual(rc, convert_mining.EXIT_FAILURE)
        self.assertFalse((self.archive / 'people').exists())
        self.assertFalse((self.archive / 'sources').exists())
        self.assertFalse((self.archive / 'documents').exists())
        self.assertFalse((self.archive / '.cache' / 'convert_mapping.csv').exists())

    def test_converted_archive_lints_without_errors(self) -> None:
        self._apply()
        n_errors, _n_warnings, _e018 = lint.run_lint_silent(self.archive, self.config)
        self.assertEqual(n_errors, 0, 'converted archive must lint with no E-level findings')

    # ── derivation units ─────────────────────────────────────────────────────

    def test_type_heuristics(self) -> None:
        self.assertEqual(convert_mining.derive_claim_type('Born in Kansas', 'Vitals')[0], 'birth')
        self.assertEqual(convert_mining.derive_claim_type('Served in the infantry', 'Military')[0], 'military')
        self.assertEqual(convert_mining.derive_claim_type('Worked as a clerk', 'Work')[0], 'occupation')
        # Unmatched → event with the Section as subtype.
        t, sub = convert_mining.derive_claim_type('Won a county fair ribbon', 'Anecdotes')
        self.assertEqual(t, 'event')
        self.assertEqual(sub, 'anecdotes')

    def test_legacy_to_edtf(self) -> None:
        self.assertEqual(convert_mining.legacy_to_edtf('1890', '1890'), '1890')
        self.assertEqual(convert_mining.legacy_to_edtf('1880', '1885'), '1880/1885')
        self.assertEqual(convert_mining.legacy_to_edtf('1890~', '1890'), '1890~')
        self.assertIsNone(convert_mining.legacy_to_edtf('', ''))
        self.assertIsNone(convert_mining.legacy_to_edtf('unknown', ''))

    def test_legacy_to_edtf_unknown_digit_decade(self) -> None:
        # TOOLING §11: legacy `??`/`?` unknown-final-digit markers -> EDTF `X`.
        self.assertEqual(convert_mining.legacy_to_edtf('189?', ''), '189X')
        self.assertEqual(convert_mining.legacy_to_edtf('189??', ''), '189X')
        self.assertEqual(convert_mining.legacy_to_edtf('', '189??'), '189X')
        self.assertEqual(convert_mining.legacy_to_edtf('189??', '189??'), '189X')

    def test_legacy_to_edtf_disagreeing_decade_interval(self) -> None:
        # Earliest/Latest land in different decades, each with an unknown
        # final digit -> the EDTF decade interval, not a dropped date.
        self.assertEqual(convert_mining.legacy_to_edtf('189?', '190?'), '189X/190X')

    def test_legacy_to_edtf_mixed_decade_and_year_interval(self) -> None:
        # One side an unknown-digit decade, the other a concrete year ->
        # a decade/year interval, not a silently narrowed concrete-only date.
        self.assertEqual(convert_mining.legacy_to_edtf('189?', '1900'), '189X/1900')
        self.assertEqual(convert_mining.legacy_to_edtf('1890', '190?'), '1890/190X')

    def test_run_convert_returns_warnings_exit_code_for_lossy_plan(self) -> None:
        # A lossy plan (missing transcript, blank Claim cell, unknown source
        # ref, ...) must not report a clean exit - automation driving this
        # tool needs to see that something was skipped.
        sources_path = self.archive / 'mining' / 'sources.txt'
        text = sources_path.read_text(encoding='utf-8')
        sources_path.write_text(
            text + '\n\nS999\ntitle: Ghost Interview\ninterviewee: Mary Hartley\n'
            'transcript: does-not-exist.txt\n',
            encoding='utf-8',
        )
        rc = convert_mining.run_convert(self.archive, self.config, apply=False)
        self.assertEqual(rc, EXIT_WARNINGS)

    def test_missing_mining_dir_errors(self) -> None:
        empty = self.archive.parent / 'empty'
        empty.mkdir()
        (empty / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        rc = convert_mining.run_convert(empty, load_fha_yaml(empty, strict=True), apply=False)
        self.assertEqual(rc, EXIT_ERRORS)

    def test_parse_aliases_rejects_non_person_id(self) -> None:
        with self.assertRaises(convert_mining.ConvertError):
            convert_mining.parse_aliases('Mary Hartley = S-a1a1a1a1a1\n')

    def test_alias_dedup_stub_by_pid(self) -> None:
        # Two alias names pointing at the same unminted P-id must yield one stub.
        aliases_path = self.archive / 'mining' / 'aliases.txt'
        existing = aliases_path.read_text(encoding='utf-8') if aliases_path.is_file() else ''
        aliases_path.write_text(
            existing + '\nAunt Sue = P-deadbeef00\nSusan = P-deadbeef00\n', encoding='utf-8',
        )
        (self.archive / 'mining' / 'stories.txt').write_text(
            '## Aunt Sue (S001)\nShe brought pie to every reunion.\n', encoding='utf-8',
        )
        plan = convert_mining.build_plan(self.archive, self.config, self.archive / 'mining')
        stub_pids = [p.pid.lower() for p in plan.stub_people]
        self.assertEqual(stub_pids.count('p-deadbeef00'), 1)

    def test_story_only_person_added_to_source_people(self) -> None:
        (self.archive / 'mining' / 'stories.txt').write_text(
            '## Uncle Theo (S001)\nHe told the same joke every Thanksgiving.\n',
            encoding='utf-8',
        )
        plan = convert_mining.build_plan(self.archive, self.config, self.archive / 'mining')
        s001 = next(s for s in plan.sources if s.legacy_id == 'S001')
        theo_pid = next(p.pid for p in plan.stub_people if p.name == 'Uncle Theo')
        self.assertIn(theo_pid, s001.people)

    def test_missing_transcript_omits_files_block(self) -> None:
        sources_path = self.archive / 'mining' / 'sources.txt'
        text = sources_path.read_text(encoding='utf-8')
        sources_path.write_text(
            text + '\n\nS999\ntitle: Ghost Interview\ninterviewee: Mary Hartley\n'
            'transcript: does-not-exist.txt\n',
            encoding='utf-8',
        )
        plan = convert_mining.build_plan(self.archive, self.config, self.archive / 'mining')
        ghost = next(s for s in plan.sources if s.legacy_id == 'S999')
        rendered = convert_mining._render_source_record(ghost)
        self.assertNotIn('files:', rendered)

    def test_transcript_path_traversal_refused(self) -> None:
        # A malformed/hostile `transcript:` value that escapes mining/transcripts/
        # (absolute path or `../`) must not be read or copied - treat it like a
        # missing transcript, not a pointer to an arbitrary local file.
        secret = self.archive.parent / 'secret.txt'
        secret.write_text('private', encoding='utf-8')
        sources_path = self.archive / 'mining' / 'sources.txt'
        text = sources_path.read_text(encoding='utf-8')
        sources_path.write_text(
            text + f'\n\nS998\ntitle: Escape Attempt\ninterviewee: Mary Hartley\n'
            f'transcript: {secret}\n',
            encoding='utf-8',
        )
        plan = convert_mining.build_plan(self.archive, self.config, self.archive / 'mining')
        escapee = next(s for s in plan.sources if s.legacy_id == 'S998')
        self.assertFalse(escapee.transcript_src.is_file())
        self.assertTrue(any('S998' in w and 'outside' in w for w in plan.warnings))
        rendered = convert_mining._render_source_record(escapee)
        self.assertNotIn('files:', rendered)

    def test_preflight_apply_handles_external_root_conflict(self) -> None:
        # A planned destination outside the archive root must not crash with
        # ValueError from Path.relative_to; it should surface as a ConvertError.
        outside = Path(self._tmp.name) / 'outside.md'
        outside.write_text('exists', encoding='utf-8')
        plan = convert_mining.ConversionPlan(
            archive_root=self.archive, sources=[], stub_people=[], questions=[],
            mapping_rows=[], warnings=[],
        )
        with mock.patch.object(convert_mining, '_record_path', return_value=outside):
            plan.sources.append(mock.Mock(transcript_src=Path('/nonexistent')))
            with self.assertRaises(convert_mining.ConvertError):
                convert_mining._preflight_apply(plan)


if __name__ == '__main__':
    unittest.main()
