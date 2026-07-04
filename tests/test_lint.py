"""
test_lint.py - fha lint forgiving-input behavior (PR 05).

Covers the "forgiving, not fussy" rule (AGENTS.md → "Who you serve"): a human
who hand-edits a claim and writes a loose date ("circa 1870", "1870s") or types
a place name into the `place:` field should be understood, not hard-rejected.
Only a genuinely unreadable date is a hard E014 error, and even then with a
plain, example-bearing message.

Also covers three graduation-path contracts:
  - GENERATED views and README.md files are never classified as id-less
    hand-authored records (so `--fix-ids` can never convert a couple folder's
    sources-index.md into a phantom person record);
  - claim `persons:` references resolve through the alias map before E005
    judges them (TOOLING §3: an unresolved non-ID name is an inert note-link,
    not a finding);
  - `--fix-ids` also mints ids into id-less claims (and stamps `reviewed:` on
    the hand-accepted ones), surgically, preserving formatting.

Like test_report.py, this builds a tiny real archive tree and calls lint's tool
logic directly (`_run_lint_core` / `run_lint`) rather than going through the
CLI, so the checks run over a fresh in-memory registry with no prior `fha index`.
"""

import datetime
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import lint
from _lib import normalize_date, read_record


_PERSON_MD = '''---
id: P-1111111111
name: Jane Doe
living: false
---

## Biography

Some text.
'''


def _source_md(claim_date: str, place_line: str = '') -> str:
    """A one-claim source whose claim date / place can be parameterised."""
    place = f'  {place_line}\n' if place_line else ''
    return (
        '---\n'
        'id: S-1111111111\n'
        'title: Test source\n'
        'source_type: other\n'
        '---\n\n'
        '## Claims\n\n'
        '```yaml\n'
        '- value: a fact\n'
        '  id: C-1111111111\n'
        '  type: birth\n'
        '  persons: [P-1111111111]\n'
        f'  date: {claim_date}\n'
        f'{place}'
        '  status: suggested\n'
        '```\n'
    )


class NormalizeDateTests(unittest.TestCase):
    """Unit tests for the loose-date translator that the lint checks rely on."""

    def test_approximate_forms_map_to_tilde(self) -> None:
        for raw in ('circa 1870', 'ca 1870', 'c. 1870', 'abt 1870',
                    'about 1870', 'around 1870', '~1870', 'est 1870'):
            self.assertEqual(normalize_date(raw), '1870~', raw)

    def test_decade_forms_map_to_x(self) -> None:
        self.assertEqual(normalize_date('1870s'), '187X')
        self.assertEqual(normalize_date("1870's"), '187X')
        self.assertEqual(normalize_date('187x'), '187X')

    def test_uncertain_and_before_and_interval(self) -> None:
        self.assertEqual(normalize_date('maybe 1900'), '1900?')
        self.assertEqual(normalize_date('before 1920'), '[..1920]')
        self.assertEqual(normalize_date('by 1920'), '[..1920]')
        self.assertEqual(normalize_date('between 1870 and 1875'), '1870/1875')
        self.assertEqual(normalize_date('1870-1875'), '1870/1875')

    def test_month_name_forms(self) -> None:
        self.assertEqual(normalize_date('June 1923'), '1923-06')
        self.assertEqual(normalize_date('Jun. 1923'), '1923-06')
        self.assertEqual(normalize_date('June 14, 1923'), '1923-06-14')
        self.assertEqual(normalize_date('the 14th of June 1923'), '1923-06-14')
        self.assertEqual(normalize_date('about June 1923'), '1923-06~')

    def test_already_canonical_passes_through_unchanged(self) -> None:
        for canon in ('1870', '1870~', '187X', '1850-05', '1850-05-20',
                      '[..1920]', '1871-02/1871-03'):
            self.assertEqual(normalize_date(canon), canon, canon)

    def test_genuinely_unparseable_returns_none(self) -> None:
        for raw in ('the day after never', 'garbage', '', '   ', None):
            self.assertIsNone(normalize_date(raw), repr(raw))


class LintForgivingDateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'sources' / 'notes').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('root_person: P-1111111111\n', encoding='utf-8')
        (self.root / 'people' / 'stubs' / 'doe__jane_P-1111111111.md').write_text(
            _PERSON_MD, encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _lint(self, claim_date: str, place_line: str = '') -> list:
        (self.root / 'sources' / 'notes' / 'test_S-1111111111.md').write_text(
            _source_md(claim_date, place_line), encoding='utf-8')
        findings, _ = lint._run_lint_core(self.root, {})
        return findings

    def _codes_for(self, findings, substring: str) -> list:
        return [f.code for f in findings if substring in f.message]

    def test_loose_date_warns_does_not_error(self) -> None:
        findings = self._lint('circa 1870')
        date_findings = [f for f in findings if 'date' in f.message and "'circa 1870'" in f.message]
        self.assertTrue(date_findings)
        self.assertTrue(all(f.severity == 'W' and f.code == 'W109' for f in date_findings))
        self.assertFalse(any(f.code == 'E014' for f in findings))
        # The suggestion names the canonical form and its plain meaning.
        self.assertIn("'1870~'", date_findings[0].message)
        self.assertIn('about 1870', date_findings[0].message)

    def test_decade_date_warns_with_x_form(self) -> None:
        findings = self._lint('1870s')
        msgs = [f.message for f in findings if f.code == 'W109' and "'1870s'" in f.message]
        self.assertTrue(msgs)
        self.assertIn("'187X'", msgs[0])
        self.assertIn('the 1870s', msgs[0])

    def test_broken_date_is_single_plain_error(self) -> None:
        findings = self._lint('the day after never')
        e014 = [f for f in findings if f.code == 'E014']
        self.assertEqual(len(e014), 1)
        msg = e014[0].message
        self.assertIn('the day after never', msg)
        # Plain, example-bearing - no bare jargon, names accepted shapes.
        self.assertIn('1880', msg)
        self.assertNotIn('EDTF', msg)

    def test_freetext_place_warns_points_to_place_text(self) -> None:
        findings = self._lint('1870', place_line='place: Fairview, Ohio')
        place_w = [f for f in findings
                   if f.code == 'W109' and 'Fairview, Ohio' in f.message]
        self.assertTrue(place_w)
        self.assertIn('place_text', place_w[0].message)
        # A typed place name is never a hard error.
        self.assertFalse(any('Fairview, Ohio' in f.message and f.severity == 'E'
                             for f in findings))

    def test_unregistered_l_id_place_still_errors(self) -> None:
        # A well-formed L-id that resolves to nothing is a broken link, not a
        # forgiving case - integrity matters, so it stays E004.
        findings = self._lint('1870', place_line='place: L-cccccccccc')
        e004 = [f for f in findings if f.code == 'E004' and 'L-cccccccccc' in f.message]
        self.assertTrue(e004)


class LintControlledVocabularyTests(unittest.TestCase):
    """E010 confidence presence + E019 status/confidence value checks (SPEC §8.1/§8.5),
    and the SPEC §9 MERGED-INTO tombstone filename grammar."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'sources' / 'notes').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('root_person: P-1111111111\n', encoding='utf-8')
        (self.root / 'people' / 'stubs' / 'doe__jane_P-1111111111.md').write_text(
            _PERSON_MD, encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _lint_claim(self, *, status: str = 'accepted', confidence: str | None = 'high') -> list:
        src = (
            '---\nid: S-1111111111\ntitle: Test source\nsource_type: other\n---\n\n'
            '## Claims\n\n```yaml\n'
            '- value: a fact\n  id: C-1111111111\n  type: birth\n'
            '  persons: [P-1111111111]\n  date: 1870\n'
            f'  status: {status}\n'
            + (f'  confidence: {confidence}\n' if confidence is not None else '')
            + ('  reviewed: 2020-01-01\n' if status == 'accepted' else '')
            + '```\n'
        )
        (self.root / 'sources' / 'notes' / 'test_S-1111111111.md').write_text(src, encoding='utf-8')
        findings, _ = lint._run_lint_core(self.root, {})
        return findings

    def test_valid_status_and_confidence_clean(self) -> None:
        findings = self._lint_claim(status='accepted', confidence='high')
        self.assertFalse([f for f in findings if f.code == 'E019'])
        self.assertFalse([f for f in findings if f.code == 'E010' and 'confidence' in f.message])

    def test_missing_confidence_is_e010(self) -> None:
        findings = self._lint_claim(confidence=None)
        self.assertTrue([f for f in findings if f.code == 'E010' and 'confidence' in f.message])

    def test_invalid_status_is_e019(self) -> None:
        findings = self._lint_claim(status='acccepted')
        e019 = [f for f in findings if f.code == 'E019' and 'status' in f.message]
        self.assertTrue(e019)
        self.assertIn('acccepted', e019[0].message)

    def test_invalid_confidence_is_e019(self) -> None:
        findings = self._lint_claim(confidence='very-high')
        self.assertTrue([f for f in findings if f.code == 'E019' and 'confidence' in f.message])

    def test_merged_into_tombstone_filename_matches_grammar(self) -> None:
        self.assertTrue(lint._PERSON_FILENAME_RE.fullmatch(
            'MERGED-INTO-P-de957bcda1__hartley__thomas_P-1234567890'))
        self.assertTrue(lint._PERSON_FILENAME_RE.fullmatch('cole__margaret_P-4d5e6f7g8h'))
        self.assertFalse(lint._PERSON_FILENAME_RE.fullmatch('notaperson'))


_GENERATED_VIEW = (
    '<!-- GENERATED by fha views sources-index on 2026-01-01 '
    '- do not edit; regenerate instead -->\n\n'
    '# Sources: 010 James Brooks + Dorothy Hill\n\n'
    '## census\n- some entry\n'
)


class NeverMintableTests(unittest.TestCase):
    """GENERATED views and README.md files carry no `id:` BY DESIGN. They must
    never be listed as auto-mintable, and --fix-ids must leave them
    byte-identical - the bug converted couple-folder sources-index.md views
    into phantom person records with permanent garbage P-ids."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        couple = self.root / 'people' / '010 James Brooks + Dorothy Hill'
        couple.mkdir(parents=True)
        (couple / 'sources-index.md').write_text(_GENERATED_VIEW, encoding='utf-8')
        (self.root / 'people' / 'README.md').write_text(
            '# How this folder works\nDocumentation, not a person.\n', encoding='utf-8')
        (self.root / 'sources').mkdir()
        (self.root / 'sources' / 'README.md').write_text(
            '# Sources\nDocumentation, not a source.\n', encoding='utf-8')
        # A genuinely hand-authored id-less person, which MUST still mint.
        (couple / 'James Brooks.md').write_text(
            '---\nname: James Brooks\nliving: false\n---\n\n# James Brooks\n',
            encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_generated_and_readme_never_classified_idless(self) -> None:
        findings, reg = lint._run_lint_core(self.root, {})
        idless_names = sorted(p.name for p, _ in reg.idless_records)
        self.assertEqual(idless_names, ['James Brooks.md'])
        # And they raise no E-codes either (a README is not a bad filename).
        offenders = [f for f in findings if f.severity == 'E'
                     and ('README' in f.path or 'sources-index' in f.path)]
        self.assertEqual(offenders, [])

    def test_mintable_report_lists_only_the_hand_authored_record(self) -> None:
        result = lint.run_lint(self.root, {})
        self.assertEqual(len(result.data['mintable']), 1)
        self.assertIn('James Brooks.md', result.data['mintable'][0])

    def test_fix_ids_leaves_generated_and_readme_byte_identical(self) -> None:
        gen = self.root / 'people' / '010 James Brooks + Dorothy Hill' / 'sources-index.md'
        readme_p = self.root / 'people' / 'README.md'
        readme_s = self.root / 'sources' / 'README.md'
        before = {p: p.read_bytes() for p in (gen, readme_p, readme_s)}

        lint.run_lint(self.root, {}, fix_ids=True)

        for p, content in before.items():
            self.assertTrue(p.exists(), f'{p} was renamed or deleted')
            self.assertEqual(p.read_bytes(), content, f'{p} was modified')
        # The genuine hand-authored record still minted and renamed as before.
        couple = self.root / 'people' / '010 James Brooks + Dorothy Hill'
        minted = [p.name for p in couple.glob('brooks__james_P-*.md')]
        self.assertEqual(len(minted), 1, sorted(p.name for p in couple.iterdir()))


_NAMED_PERSON = '''---
id: P-1111111111
name: Sam Rivera
living: false
---

# Sam Rivera
'''


def _claims_source(claims_yaml: str) -> str:
    return (
        '---\nid: S-1111111111\ntitle: Test source\nsource_type: other\n---\n\n'
        '## Claims\n\n```yaml\n' + claims_yaml + '```\n'
    )


class ClaimPersonAliasResolutionTests(unittest.TestCase):
    """E005 judges claim persons: AFTER alias resolution (TOOLING §3 E004:
    "resolved through the alias map first"): a name that resolves is not an
    error; an unresolvable or ambiguous name is an inert note-link (not an
    E005 dead end); a bare or wrapped P-id that names no record stays E005,
    with a fix (`fha stubs`) that now actually works on wrapped refs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir()
        (self.root / 'people' / 'rivera__sam_P-1111111111.md').write_text(
            _NAMED_PERSON, encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _lint_with_claims(self, claims_yaml: str) -> list:
        (self.root / 'sources' / 'test_S-1111111111.md').write_text(
            _claims_source(claims_yaml), encoding='utf-8')
        findings, _ = lint._run_lint_core(self.root, {})
        return findings

    def test_resolvable_name_link_is_not_e005(self) -> None:
        findings = self._lint_with_claims(
            '- id: C-1111111111\n  type: birth\n  persons: ["[[Sam Rivera]]"]\n'
            '  value: born 1985\n  status: suggested\n  confidence: high\n')
        self.assertEqual([f for f in findings if f.code == 'E005'], [])

    def test_ambiguous_name_is_inert_not_e005(self) -> None:
        # A second Sam Rivera makes the name ambiguous: the claim's link is an
        # inert note-link (no E005, no guess), and the clash surfaces as the
        # alias-layer warning (W113 active clash), a human-detangle nudge.
        (self.root / 'people' / 'rivera__sam_P-2222222222.md').write_text(
            _NAMED_PERSON.replace('P-1111111111', 'P-2222222222'), encoding='utf-8')
        findings = self._lint_with_claims(
            '- id: C-1111111111\n  type: birth\n  persons: ["[[Sam Rivera]]"]\n'
            '  value: born 1985\n  status: suggested\n  confidence: high\n')
        self.assertEqual([f for f in findings if f.severity == 'E'], [])
        self.assertTrue([f for f in findings if f.code == 'W113'])

    def test_wrapped_missing_pid_is_e005_with_working_fix(self) -> None:
        findings = self._lint_with_claims(
            '- id: C-1111111111\n  type: birth\n  persons: ["[[P-9999999999|Ghost]]"]\n'
            '  value: born 1985\n  status: suggested\n  confidence: high\n')
        # Two sites see the wrapped token (the prose token scan and the claim
        # persons: check); both must report the clean id and the working fix.
        e005 = [f for f in findings if f.code == 'E005']
        self.assertTrue(e005)
        claim_e005 = [f for f in e005 if 'Claim' in f.message]
        self.assertEqual(len(claim_e005), 1)
        for f in e005:
            self.assertIn('9999999999', f.message)
            self.assertIn('fha stubs', f.message)
            self.assertNotIn('[[', f.message)   # no bracket garbage in the id

    def test_bare_missing_pid_stays_e005(self) -> None:
        findings = self._lint_with_claims(
            '- id: C-1111111111\n  type: birth\n  persons: [P-9999999999]\n'
            '  value: born 1985\n  status: suggested\n  confidence: high\n')
        self.assertTrue([f for f in findings if f.code == 'E005'])

    def test_wrapped_corroborates_target_resolves(self) -> None:
        # `corroborates: ["[[C-…]]"]` must be checked as its bare C-id, not as
        # bracket garbage that can never match a known id.
        findings = self._lint_with_claims(
            '- id: C-1111111111\n  type: birth\n  persons: [P-1111111111]\n'
            '  value: born 1985\n  status: suggested\n  confidence: high\n'
            '- id: C-2222222222\n  type: birth\n  persons: [P-1111111111]\n'
            '  value: also born 1985\n  status: suggested\n  confidence: high\n'
            '  corroborates: ["[[C-1111111111]]"]\n')
        self.assertEqual([f for f in findings if f.code == 'E004'], [])


class ClaimIdMintingTests(unittest.TestCase):
    """--fix-ids mints `id:` into id-less claims (the "linter mints on contact"
    doctrine applied to claims) and stamps `reviewed:` on the hand-accepted
    ones among them, by pure text insertion - sibling lines byte-identical."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir()
        (self.root / 'people' / 'rivera__sam_P-1111111111.md').write_text(
            _NAMED_PERSON, encoding='utf-8')
        self.src = self.root / 'sources' / 'test_S-1111111111.md'
        self.src.write_text(_claims_source(
            '- value: "born 1985"\n'
            '  type: birth\n'
            '  persons: ["[[Sam Rivera]]"]\n'
            '  status: accepted\n'
            '  confidence: high\n'
            '\n'
            '- value: "a hunch"\n'
            '  type: note\n'
            '  persons: [P-1111111111]\n'
            '  status: suggested\n'
            '  confidence: low\n'
        ), encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_dry_run_previews_and_writes_nothing(self) -> None:
        before = self.src.read_bytes()
        result = lint.run_lint(self.root, {}, fix_ids=True, dry_run=True)
        self.assertEqual(self.src.read_bytes(), before)
        self.assertEqual(result.changed, [])
        preview = [l for l in result.data['progress'] if 'claim id' in l]
        self.assertEqual(len(preview), 1)
        self.assertIn('would mint 2 claim id(s)', preview[0])
        self.assertIn('reviewed:', preview[0])   # the stamp is previewed too

    def test_minting_inserts_ids_and_stamps_reviewed_surgically(self) -> None:
        before_lines = self.src.read_text(encoding='utf-8').splitlines()
        result = lint.run_lint(self.root, {}, fix_ids=True)
        self.assertIn(str(self.src), result.changed)

        text = self.src.read_text(encoding='utf-8')
        # Every original line survives byte-for-byte (insertion-only surgery;
        # the first field of each claim moves down one line, bytes untouched).
        after_lines = text.splitlines()
        for line in before_lines:
            if line.startswith('- '):
                self.assertIn('  ' + line[2:], after_lines, line)
            else:
                self.assertIn(line, after_lines, line)

        rec = read_record(self.src)
        claims = rec['claims']
        self.assertEqual(len(claims), 2)
        for c in claims:
            self.assertTrue(str(c.get('id', '')).lower().startswith('c-'), c)
        today = datetime.date.today().isoformat()
        accepted = next(c for c in claims if c['status'] == 'accepted')
        suggested = next(c for c in claims if c['status'] == 'suggested')
        # The hand-accepted claim gets today's reviewed: stamp (TOOLING §3b:
        # directing the tool is the human's accept); the suggested one must not.
        self.assertEqual(str(accepted.get('reviewed', '')), today)
        self.assertFalse(suggested.get('reviewed'))

        # The graduated file now lints with no claim-shaped E-codes at all.
        findings, _ = lint._run_lint_core(self.root, {})
        self.assertEqual(
            [f for f in findings if f.severity == 'E'
             and f.code in ('E005', 'E006', 'E010')], [])

    def test_flow_style_claim_is_refused_not_corrupted(self) -> None:
        self.src.write_text(_claims_source(
            '- {value: one liner, type: note, persons: [P-1111111111], '
            'status: suggested, confidence: low}\n'), encoding='utf-8')
        before = self.src.read_bytes()
        result = lint.run_lint(self.root, {}, fix_ids=True)
        self.assertEqual(self.src.read_bytes(), before)
        refusals = [l for l in result.data['progress'] if 'fha id mint C' in l]
        self.assertEqual(len(refusals), 1)

    def test_claims_with_ids_already_are_left_alone(self) -> None:
        self.src.write_text(_claims_source(
            '- id: C-1111111111\n  value: done\n  type: note\n'
            '  persons: [P-1111111111]\n  status: suggested\n  confidence: low\n'
        ), encoding='utf-8')
        before = self.src.read_bytes()
        lint.run_lint(self.root, {}, fix_ids=True)
        self.assertEqual(self.src.read_bytes(), before)


def _person_md(pid: str, name: str, extra: str = '') -> str:
    return (
        f'---\nid: {pid}\nname: {name}\nliving: false\n{extra}---\n\n# {name}\n'
    )


class HyphenatedNameFilenameTests(unittest.TestCase):
    """Fix for E002 on hyphenated names: SPEC §13 never forbids hyphens, and
    `hartley__mary-jane` / `smith-jones__anne` are ordinary names. They must
    lint clean (no E002, no W117), companion filenames included, and the
    companion-kind classification must be untouched by hyphens in name slots."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _lint(self):
        return lint._run_lint_core(self.root, {})

    def test_hyphenated_given_name_profile_lints_clean(self) -> None:
        (self.root / 'people' / 'hartley__mary-jane_P-1111111111.md').write_text(
            _person_md('P-1111111111', 'Mary-Jane Hartley'), encoding='utf-8')
        findings, _ = self._lint()
        self.assertEqual([f for f in findings if f.code in ('E002', 'W117')], [])

    def test_hyphenated_surname_profile_lints_clean(self) -> None:
        (self.root / 'people' / 'smith-jones__anne_P-2222222222.md').write_text(
            _person_md('P-2222222222', 'Anne Smith-Jones'), encoding='utf-8')
        findings, _ = self._lint()
        self.assertEqual([f for f in findings if f.code in ('E002', 'W117')], [])

    def test_hyphenated_companion_filenames_lint_clean_and_classify(self) -> None:
        (self.root / 'people' / 'smith-jones__anne_P-2222222222.md').write_text(
            _person_md('P-2222222222', 'Anne Smith-Jones'), encoding='utf-8')
        (self.root / 'people' / 'smith-jones__anne_research_P-2222222222.md').write_text(
            '---\nid: P-2222222222\n---\n\n## Research Notes\n*(none yet)*\n',
            encoding='utf-8')
        (self.root / 'people' / 'hartley__mary-jane_P-1111111111.md').write_text(
            _person_md('P-1111111111', 'Mary-Jane Hartley'), encoding='utf-8')
        (self.root / 'people' / 'hartley__mary-jane_timeline_P-1111111111.md').write_text(
            '---\nid: P-1111111111\n---\n\n# Timeline\n', encoding='utf-8')
        findings, reg = self._lint()
        self.assertEqual([f for f in findings if f.code in ('E002', 'W117')], [])
        # Companion kind classification survives hyphenated name slots: the
        # files register as companions of their person, not as new profiles.
        self.assertIn('p-2222222222', reg.person_companion_paths)
        self.assertIn('p-1111111111', reg.person_companion_paths)
        self.assertEqual([f for f in findings if f.code == 'E001'], [])

    def test_surname_less_hyphenated_given_lints_clean(self) -> None:
        (self.root / 'people' / '__mary-jane_P-3333333333.md').write_text(
            _person_md('P-3333333333', 'Mary-Jane'), encoding='utf-8')
        findings, _ = self._lint()
        self.assertEqual([f for f in findings if f.code in ('E002', 'W117')], [])

    def test_missing_separator_is_still_w117_never_e002(self) -> None:
        # A single-underscore name still gets the gentle W117 nudge, not an error.
        (self.root / 'people' / 'smith-jones_anne_P-4444444444.md').write_text(
            _person_md('P-4444444444', 'Anne Smith-Jones'), encoding='utf-8')
        findings, _ = self._lint()
        self.assertEqual([f for f in findings if f.code == 'E002'], [])
        self.assertTrue([f for f in findings if f.code == 'W117'])

    def test_kind_suffix_files_still_classify_as_companions(self) -> None:
        # The hyphen-bearing kind (`sources-index`) keeps working, and a given
        # name may not swallow it: classification is parse_filename's endswith.
        (self.root / 'people' / 'cole__margaret_P-5555555555.md').write_text(
            _person_md('P-5555555555', 'Margaret Cole'), encoding='utf-8')
        (self.root / 'people' / 'cole__margaret_sources-index_P-5555555555.md').write_text(
            '---\nid: P-5555555555\n---\n\n# Sources\n', encoding='utf-8')
        findings, reg = self._lint()
        self.assertEqual([f for f in findings if f.code in ('E002', 'W117', 'E001')], [])
        companion_names = [p.name for p in reg.person_companion_paths.get('p-5555555555', [])]
        self.assertEqual(companion_names, ['cole__margaret_sources-index_P-5555555555.md'])


class ResearchHypothesisE004Tests(unittest.TestCase):
    """Fix for the E004 false positive on research-file hypotheses: SPEC §16
    homes `## Hypotheses` in `…_research_P-….md`, and index.py indexes them
    from there - so a `[[H-…]]` cite of one must resolve. A genuinely dangling
    H-id, or a mere citation with no definition, stays E004."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_profile(self, body_line: str) -> None:
        (self.root / 'people' / 'hartley__thomas_P-1111111111.md').write_text(
            '---\nid: P-1111111111\nname: Thomas Hartley\nliving: false\n---\n\n'
            f'# Thomas Hartley\n\n## Biography\n{body_line}\n', encoding='utf-8')

    def _write_research(self, body: str) -> None:
        (self.root / 'people' / 'hartley__thomas_research_P-1111111111.md').write_text(
            '---\nid: P-1111111111\n---\n\n' + body, encoding='utf-8')

    def test_research_defined_hypothesis_cited_from_profile_is_not_e004(self) -> None:
        self._write_profile('Working theory: [[H-abcabcabca]] covers the arrival.')
        self._write_research(
            '## Research Notes\n*(none yet)*\n\n'
            '## Hypotheses\n\n'
            '- id: H-abcabcabca\n'
            '  hypothesis: "arrived by ~1869"\n'
            '  basis: "railroad boom"\n'
            '  origin: agent\n'
            '  status: open\n')
        findings, reg = lint._run_lint_core(self.root, {})
        self.assertIn('h-abcabcabca', reg.hypothesis_ids)
        self.assertEqual([f for f in findings if f.code == 'E004'], [])

    def test_genuinely_dangling_hypothesis_is_still_e004(self) -> None:
        self._write_profile('Working theory: [[H-9999999999]] covers the arrival.')
        self._write_research('## Hypotheses\n\n*(none yet)*\n')
        findings, _ = lint._run_lint_core(self.root, {})
        e004 = [f for f in findings if f.code == 'E004' and 'h-9999999999' in f.message]
        self.assertTrue(e004)

    def test_citation_in_research_body_is_not_a_definition(self) -> None:
        # A [[H-…]] reference OUTSIDE the ## Hypotheses entries (a research-log
        # question, prose) is a cite, not a record - it must not self-resolve.
        self._write_profile('Nothing hypothetical here.')
        self._write_research(
            '## Research Log\n\n'
            '- date: 2026-06-12\n'
            '  question: "[[H-7777777777]] arrival window"\n'
            '  result: nil\n')
        findings, reg = lint._run_lint_core(self.root, {})
        self.assertNotIn('h-7777777777', reg.hypothesis_ids)
        self.assertTrue([f for f in findings
                         if f.code == 'E004' and 'h-7777777777' in f.message])


_PLACEHOLDER_PERSON = '''---
id: P-__________   # OPTIONAL - LINT WILL CREATE FOR YOU LATER IF MISSING
aliases:           # OPTIONAL - the code, repeated
  - P-__________   # paste the same code here too
name: Thomas Hartley
living: false
created: 2026-01-01
tier: stub
---

# Thomas Hartley
'''

_PLACEHOLDER_SOURCE = '''---
id: S-__________   # OPTIONAL - LINT WILL CREATE FOR YOU LATER IF MISSING
aliases:
  - S-__________   # paste the same code here too
title: 1880 census
source_type: census
created: 2026-01-01
---

## Claims
```yaml
- value: "Thomas Hartley, living in Fairview"
  type: residence
  persons: ["[[Thomas Hartley]]"]
  id: C-__________         # this claim's own 10-character code
  status: suggested
  confidence: medium
```
'''


class PlaceholderIdTests(unittest.TestCase):
    """The shipped templates' placeholder ids (`P-__________`, `S-__________`,
    `C-__________`) promise "LINT WILL CREATE FOR YOU LATER IF MISSING", so a
    template copy still carrying one is auto-mintable, never E002: --fix-ids
    replaces the placeholder in place (id line, aliases entry, claim id) and
    the file lints clean afterwards."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir()
        self.person = self.root / 'people' / 'thomas hartley.md'
        self.source = self.root / 'sources' / '1880 census.md'
        self.person.write_text(_PLACEHOLDER_PERSON, encoding='utf-8')
        self.source.write_text(_PLACEHOLDER_SOURCE, encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_placeholder_ids_classify_as_idless_never_e002(self) -> None:
        findings, reg = lint._run_lint_core(self.root, {})
        self.assertEqual([f for f in findings if f.severity == 'E'], [])
        kinds = {p.name: k for p, k in reg.idless_records}
        self.assertEqual(kinds, {'thomas hartley.md': 'P', '1880 census.md': 'S'})
        self.assertEqual({p.name for p in reg.placeholder_id_paths},
                         {'thomas hartley.md', '1880 census.md'})

    def test_mintable_listing_says_placeholder_will_be_replaced(self) -> None:
        result = lint.run_lint(self.root, {})
        self.assertEqual(len(result.data['mintable']), 2)
        for line in result.data['mintable']:
            self.assertIn('template placeholder', line)
            self.assertIn('--fix-ids', line)

    def test_dry_run_previews_replacement_and_writes_nothing(self) -> None:
        before = {p: p.read_bytes() for p in (self.person, self.source)}
        result = lint.run_lint(self.root, {}, fix_ids=True, dry_run=True)
        for p, content in before.items():
            self.assertEqual(p.read_bytes(), content)
        self.assertEqual(result.changed, [])
        previews = [l for l in result.data['progress']
                    if 'replacing the template placeholder id' in l]
        self.assertEqual(len(previews), 2)

    def test_fix_ids_replaces_placeholders_and_file_lints_clean(self) -> None:
        lint.run_lint(self.root, {}, fix_ids=True)

        minted_people = list((self.root / 'people').glob('hartley__thomas_P-*.md'))
        minted_sources = list((self.root / 'sources').glob('1880-census_S-*.md'))
        self.assertEqual(len(minted_people), 1)
        self.assertEqual(len(minted_sources), 1)

        person_text = minted_people[0].read_text(encoding='utf-8')
        self.assertNotIn('P-__________', person_text)
        # Surgical: the id value changed on its own line; the teaching comment
        # and the aliases entry survive, now carrying the real code.
        pid = read_record(minted_people[0])['meta']['id']
        self.assertIn(f'id: {pid}   # OPTIONAL', person_text)
        self.assertIn(f'- {pid}   # paste the same code here too', person_text)

        source_text = minted_sources[0].read_text(encoding='utf-8')
        self.assertNotIn('S-__________', source_text)
        self.assertNotIn('C-__________', source_text)
        claim = read_record(minted_sources[0])['claims'][0]
        self.assertTrue(str(claim['id']).lower().startswith('c-'))
        self.assertIn("# this claim's own 10-character code", source_text)

        findings, _ = lint._run_lint_core(self.root, {})
        self.assertEqual([f for f in findings if f.severity == 'E'], [])

    def test_malformed_but_not_placeholder_id_stays_e002(self) -> None:
        self.person.write_text(_PLACEHOLDER_PERSON.replace(
            'P-__________   # OPTIONAL - LINT WILL CREATE FOR YOU LATER IF MISSING',
            'P-123'), encoding='utf-8')
        findings, reg = lint._run_lint_core(self.root, {})
        e002 = [f for f in findings if f.code == 'E002' and 'P-123' in f.message]
        self.assertTrue(e002)
        self.assertNotIn('thomas hartley.md', {p.name for p, _ in reg.idless_records})

    def test_placeholder_with_real_filename_id_is_e003_paste_nudge(self) -> None:
        # The filename already carries the code; the fix is a paste, not a mint.
        target = self.root / 'people' / 'hartley__thomas_P-5555555555.md'
        self.person.rename(target)
        findings, reg = lint._run_lint_core(self.root, {})
        self.assertEqual([f for f in findings
                          if f.code == 'E002' and 'hartley__thomas' in f.path], [])
        e003 = [f for f in findings if f.code == 'E003' and 'placeholder' in f.message]
        self.assertEqual(len(e003), 1)
        self.assertIn('P-5555555555', e003[0].message)
        self.assertNotIn(target.name, {p.name for p, _ in reg.idless_records})

    def test_placeholder_claim_id_in_real_source_is_e010_not_e002(self) -> None:
        real = self.root / 'sources' / 'census_S-1111111111.md'
        real.write_text(_PLACEHOLDER_SOURCE.replace(
            'S-__________   # OPTIONAL - LINT WILL CREATE FOR YOU LATER IF MISSING',
            'S-1111111111').replace('  - S-__________', '  - S-1111111111'),
            encoding='utf-8')
        self.source.unlink()
        findings, _ = lint._run_lint_core(self.root, {})
        self.assertEqual([f for f in findings if f.code == 'E002'], [])
        e010 = [f for f in findings if f.code == 'E010' and 'placeholder' in f.message]
        self.assertEqual(len(e010), 1)
        self.assertIn('--fix-ids', e010[0].message)

        lint.run_lint(self.root, {}, fix_ids=True)
        text = real.read_text(encoding='utf-8')
        self.assertNotIn('C-__________', text)
        findings, _ = lint._run_lint_core(self.root, {})
        self.assertEqual([f for f in findings if f.code in ('E002', 'E010')], [])


class NeedsSourcingBacklogTests(unittest.TestCase):
    """The needs-sourcing backlog lists RECORDED provisional dates only
    (TOOLING §3): a present-but-empty `death:` key records nothing, and death
    is inapplicable while a person is living or unknown-living (SPEC §8.2)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _backlog(self, fields: str) -> list:
        (self.root / 'people' / 'rivera__sam_P-1111111111.md').write_text(
            f'---\nid: P-1111111111\nname: Sam Rivera\n{fields}---\n\n# Sam Rivera\n',
            encoding='utf-8')
        return lint.run_lint(self.root, {}).data['backlog']

    def test_empty_death_key_is_not_listed(self) -> None:
        backlog = self._backlog('living: false\nbirth: 1985-04-12\ndeath:\n')
        self.assertFalse([l for l in backlog if 'death' in l], backlog)
        self.assertFalse([l for l in backlog if "'None'" in l], backlog)
        # The recorded birth is still nudged toward a source.
        self.assertTrue([l for l in backlog if 'provisional birth' in l])

    def test_living_person_with_empty_death_gets_nothing(self) -> None:
        backlog = self._backlog('living: true\ndeath:\n')
        self.assertEqual(backlog, [])

    def test_living_person_death_value_is_skipped(self) -> None:
        # Even a filled-in death is not worklisted while living: true - death
        # is inapplicable while living (SPEC §8.2).
        backlog = self._backlog('living: true\ndeath: 1941~\n')
        self.assertFalse([l for l in backlog if 'death' in l], backlog)

    def test_unknown_living_death_value_is_skipped(self) -> None:
        backlog = self._backlog('living: unknown\ndeath: 1941~\n')
        self.assertFalse([l for l in backlog if 'death' in l], backlog)

    def test_deceased_provisional_death_is_still_listed(self) -> None:
        backlog = self._backlog('living: false\ndeath: 1941~\n')
        listed = [l for l in backlog if "provisional death: '1941~'" in l]
        self.assertEqual(len(listed), 1, backlog)

    def test_living_person_provisional_birth_is_still_listed(self) -> None:
        backlog = self._backlog('living: true\nbirth: 1985~\n')
        self.assertTrue([l for l in backlog if "provisional birth: '1985~'" in l])


if __name__ == '__main__':
    unittest.main()
