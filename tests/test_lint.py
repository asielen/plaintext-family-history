"""
test_lint.py - fha lint forgiving-input behavior (PR 05).

Covers the "forgiving, not fussy" rule (AGENTS.md → "Who you serve"): a human
who hand-edits a claim and writes a loose date ("circa 1870", "1870s") or types
a place name into the `place:` field should be understood, not hard-rejected.
Only a genuinely unreadable date is a hard E014 error, and even then with a
plain, example-bearing message.

Also covers the graduation-path contracts:
  - GENERATED views and README.md files are never classified as id-less
    hand-authored records (so `--fix-ids` can never convert a couple folder's
    sources-index.md into a phantom person record);
  - claim `persons:` references resolve through the alias map before E005
    judges them (TOOLING §3: an unresolved non-ID name is an inert note-link,
    not a finding) - but a NEAR-MISS code (`P-de957bcda`, nine characters) is
    a typo to report, never silence;
  - `--fix-ids` also mints ids into id-less claims (and stamps `reviewed:` on
    the hand-accepted ones), surgically, preserving formatting - guarded so a
    bad rewrite is a refusal, never a corrupted source (blank `id:` completed
    in place, lookalikes inside block scalars never touched, anchor items
    refused, LF files stay LF, and the whole result re-parsed before writing);
  - `--fix-claims-fence` wraps only what re-reads to the same claims, and
    refuses (rather than deletes) fence-lookalike ``` lines in evidence;
  - `--fix-ids` merges the old-name aliases into an EXISTING aliases: block
    (template copies ship one), and says "(old name kept as an alias)" only
    when that actually happened.

Like test_report.py, this builds a tiny real archive tree and calls lint's tool
logic directly (`_run_lint_core` / `run_lint`) rather than going through the
CLI, so the checks run over a fresh in-memory registry with no prior `fha index`.
"""

import datetime
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import lint
from _lib import CLAIMS_RE, EXIT_WARNINGS, normalize_date, read_record


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


class LintPlacesRegistryShapeTests(unittest.TestCase):
    """places/places.yaml's top level must be a list (SPEC §15). Codex review,
    PR #150 follow-up: a valid-YAML-but-wrong-shape file (e.g. `not_a_list:
    true`) used to be silently coerced to zero places with no finding at
    all - `fha lint` said "no issues found" on exactly the archive state
    `fha claim`'s write-time place lookup (`_lib.read_places_registry`) was
    separately warning about as malformed, sending the human to a command
    that told them nothing was wrong."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'places').mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _lint(self, places_yaml_text: str) -> list:
        (self.root / 'places' / 'places.yaml').write_text(places_yaml_text, encoding='utf-8')
        findings, _ = lint._run_lint_core(self.root, {})
        return findings

    def test_non_list_top_level_is_an_e010_finding(self) -> None:
        findings = self._lint('not_a_list: true\n')
        e010 = [f for f in findings if f.code == 'E010' and 'places.yaml' in f.message]
        self.assertTrue(e010, [str(f) for f in findings])
        self.assertIn('not a list', e010[0].message)

    def test_comment_only_seed_file_is_not_a_finding(self) -> None:
        # The shipped archive-template seed (all comments, so
        # yaml.safe_load returns None) is a normal empty registry, not a
        # malformed one - must not raise an E010. Must keep passing after
        # issue #168's finding 2 fix (explicit null vs. genuinely-empty
        # now come from the source text, not the parsed value alone).
        seed_path = ROOT / 'archive-template' / 'places' / 'places.yaml'
        findings = self._lint(seed_path.read_text(encoding='utf-8'))
        self.assertFalse([f for f in findings if 'places.yaml' in f.message])

    def test_empty_file_is_not_a_finding(self) -> None:
        findings = self._lint('')
        self.assertFalse([f for f in findings if 'places.yaml' in f.message])

    def test_well_formed_list_is_not_a_finding(self) -> None:
        findings = self._lint('- id: L-aaaaaaaaaa\n  name: Fairview\n')
        self.assertFalse([f for f in findings if 'places.yaml' in f.message])

    def test_explicit_null_is_an_e010_finding(self) -> None:
        # Issue #168 finding 2 (Codex review, PR #159): an explicit `null`
        # parses to the same `None` a genuinely empty/comment-only file
        # does, but it is real hand-typed content ("nothing here yet"),
        # not the absence of any - `fha lint`'s registry check must not
        # silently wave it through as a normal empty registry.
        findings = self._lint('null\n')
        e010 = [f for f in findings if f.code == 'E010' and 'places.yaml' in f.message]
        self.assertTrue(e010, [str(f) for f in findings])
        self.assertIn('null', e010[0].message)

    def test_places_yaml_as_a_directory_is_an_e010_finding(self) -> None:
        # Adversarial review of PR #168: lint.py used to check this case
        # itself before its places-parsing block was folded into the shared
        # `_lib.read_places_registry` helper, and lost the check in the
        # process - a places.yaml that is a directory on disk used to
        # silently read back as an ordinary empty registry, no finding at
        # all, instead of telling the human what's actually wrong.
        (self.root / 'places' / 'places.yaml').mkdir()
        findings, _ = lint._run_lint_core(self.root, {})
        e010 = [f for f in findings if f.code == 'E010' and 'places.yaml' in f.message]
        self.assertTrue(e010, [str(f) for f in findings])
        self.assertIn('directory', e010[0].message)

    def test_malformed_yaml_error_message_has_no_pyyaml_jargon(self) -> None:
        # Issue #168 finding 1: `fha lint` shares the same plain-language
        # error text as the write path now (both come from
        # `_lib.read_places_registry`) - no `<unicode string>`/caret/
        # parser-internals jargon should reach the human here either.
        findings = self._lint('- id: L-aaaaaaaaaa\n  name: [unterminated\n')
        e010 = [f for f in findings if f.code == 'E010' and 'places.yaml' in f.message]
        self.assertTrue(e010, [str(f) for f in findings])
        for jargon in ('<unicode string>', '^', 'stream end', 'flow sequence'):
            self.assertNotIn(jargon, e010[0].message)


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

    def test_refusal_on_a_blank_value_names_entry_n_not_the_string_none(self) -> None:
        # `str(claim.get('value', ''))[:40] or fallback` looked like it
        # guarded a blank `value:`, but `str()` ran before the `or`: the
        # refusal line would otherwise read `claim "None"` instead of the
        # same `entry N` fallback an actually-missing value already gets.
        # Reuses the flow-style refusal path (`value:` blank inside a
        # one-line `- {...}` claim) since that is the cheapest existing
        # trigger for the refusal message this `label` feeds.
        self.src.write_text(_claims_source(
            '- {value: , type: note, persons: [P-1111111111], '
            'status: suggested, confidence: low}\n'), encoding='utf-8')
        result = lint.run_lint(self.root, {}, fix_ids=True)
        refusals = [l for l in result.data['progress'] if 'fha id mint C' in l]
        self.assertEqual(len(refusals), 1)
        self.assertIn('claim "entry 1"', refusals[0])
        self.assertNotIn('"None"', refusals[0])


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


class PersonFilenamePartsSuffixTests(unittest.TestCase):
    """`lint._person_filename_parts` is the third of issue #53's three sites:
    `--fix-ids` re-derives the SAME §13 surname/given split independently
    when it renames a hand-authored, id-less person record. It must apply
    the shared `_lib.strip_generational_suffix` rule exactly like
    `_lib.stub_slug_name` does, so a hand-typed "Roy Eugene Dodson Jr" mints
    as `dodson__roy_eugene_jr_P-….md`, not `jr__roy_eugene_dodson_P-….md`."""

    def test_suffix_is_pulled_off_the_surname_slot(self) -> None:
        self.assertEqual(
            lint._person_filename_parts('Roy Eugene Dodson Jr', 'fallback'),
            ('dodson', 'roy_eugene_jr'),
        )

    def test_father_and_son_share_the_same_surname(self) -> None:
        father = lint._person_filename_parts('Roy Eugene Dodson', 'fallback')
        son = lint._person_filename_parts('Roy Eugene Dodson Jr', 'fallback')
        self.assertEqual(father[0], son[0])
        self.assertEqual(father[0], 'dodson')

    def test_every_suffix_in_the_list_round_trips(self) -> None:
        for suffix in ('Jr', 'Jr.', 'Sr', 'Sr.', 'II', 'III', 'IV', 'V'):
            with self.subTest(suffix=suffix):
                surname, given = lint._person_filename_parts(
                    f'James Whitelock {suffix}', 'fallback')
                self.assertEqual(surname, 'whitelock')
                self.assertEqual(given, f'james_{suffix.rstrip(".").lower()}')

    def test_two_token_given_plus_suffix_has_no_promoted_surname(self) -> None:
        self.assertEqual(
            lint._person_filename_parts('Roy Jr', 'fallback'), ('', 'roy_jr'))

    def test_suffix_alone_falls_through_to_the_pre_existing_single_token_path(self) -> None:
        # Nothing left to strip TO - reads as an ordinary given name, same as
        # before this fix (this function's pre-existing single-token
        # behaviour, untouched: surname = the one word, given = 'unknown').
        self.assertEqual(
            lint._person_filename_parts('Jr', 'fallback'), ('jr', 'unknown'))

    def test_no_suffix_single_token_behaviour_is_unchanged(self) -> None:
        # This function's own (pre-existing, non-SPEC-mononym) single-token
        # convention is out of scope for #53 and must not move.
        self.assertEqual(
            lint._person_filename_parts('Cher', 'fallback'), ('cher', 'unknown'))


class FixIdsRenamesSuffixedNamesTests(unittest.TestCase):
    """End-to-end: `--fix-ids` renaming a hand-authored, id-less person file
    whose name carries a generational suffix (issue #53, confirmed by the
    reporter "fixed here by hand" on the live archive before this fix)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fix_ids_files_the_suffixed_name_under_the_surname(self) -> None:
        (self.root / 'people' / 'Roy Eugene Dodson Jr.md').write_text(
            '---\nname: Roy Eugene Dodson Jr\nliving: false\n---\n\n'
            '# Roy Eugene Dodson Jr\n', encoding='utf-8')
        lint.run_lint(self.root, {}, fix_ids=True)
        minted = [p.name for p in (self.root / 'people').glob('dodson__roy_eugene_jr_P-*.md')]
        self.assertEqual(len(minted), 1,
                          sorted(p.name for p in (self.root / 'people').iterdir()))

    def test_fix_ids_father_and_son_file_under_the_same_surname(self) -> None:
        (self.root / 'people' / 'Roy Eugene Dodson.md').write_text(
            '---\nname: Roy Eugene Dodson\nliving: false\n---\n\n'
            '# Roy Eugene Dodson\n', encoding='utf-8')
        (self.root / 'people' / 'Roy Eugene Dodson Jr.md').write_text(
            '---\nname: Roy Eugene Dodson Jr\nliving: false\n---\n\n'
            '# Roy Eugene Dodson Jr\n', encoding='utf-8')
        lint.run_lint(self.root, {}, fix_ids=True)
        people_dir = self.root / 'people'
        father = list(people_dir.glob('dodson__roy_eugene_P-*.md'))
        son = list(people_dir.glob('dodson__roy_eugene_jr_P-*.md'))
        self.assertEqual(len(father), 1, sorted(p.name for p in people_dir.iterdir()))
        self.assertEqual(len(son), 1, sorted(p.name for p in people_dir.iterdir()))


class FixIdsBlankNameFallsBackToFilenameTests(unittest.TestCase):
    """`_fix_mint_ids` read the new record's name as
    `str(read_record(path)['meta'].get('name', ''))` - the same omitted-
    vs-blank hazard again: a blank `name:` line (YAML null, key present)
    str()-converted to the literal text 'None' before this fix, and
    `_person_filename_parts` then split THAT into a bogus surname 'none'
    instead of falling back to the file's own stem, the way a genuinely
    nameless record's docstring says it should ("falling back to the
    hand-filename when there is no usable name")."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_blank_name_files_under_the_filename_stem_not_none(self) -> None:
        (self.root / 'people' / 'Mystery Person.md').write_text(
            '---\nname:\nliving: false\n---\n\n# Mystery Person\n', encoding='utf-8')
        lint.run_lint(self.root, {}, fix_ids=True)
        minted = list((self.root / 'people').glob('*_P-*.md'))
        self.assertEqual(len(minted), 1,
                          sorted(p.name for p in (self.root / 'people').iterdir()))
        self.assertNotIn('none', minted[0].name.lower())
        self.assertIn('mysteryperson', minted[0].name.lower())


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

    def test_dangling_hypothesis_e004_names_the_right_fix(self) -> None:
        # Issue #56's own complaint: the generic E004 orphan message suggests
        # `fha stubs` - a PERSON-record minting command - for what is plainly
        # an H- reference. There is no hypothesis-minting tool at all; a
        # hypothesis is defined by hand in some person's ## Hypotheses
        # section (SPEC §16), so the hint should say that instead.
        self._write_profile('Working theory: [[H-9999999999]] covers the arrival.')
        self._write_research('## Hypotheses\n\n*(none yet)*\n')
        findings, _ = lint._run_lint_core(self.root, {})
        e004 = [f for f in findings if f.code == 'E004' and 'h-9999999999' in f.message]
        self.assertTrue(e004)
        self.assertIn('## Hypotheses', e004[0].message)
        self.assertNotIn('fha stubs', e004[0].message)

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

    def test_hypothesis_defined_directly_in_a_profile_is_not_e004(self) -> None:
        # #56: the gate used to be the FILENAME (`_research_` in the stem),
        # not the content - so a well-formed ## Hypotheses section written
        # straight into a curated person's own profile (no research
        # companion at all here) was invisible to E004, and citing its H-id
        # from anywhere else in the archive drew a false "create the missing
        # record - run `fha stubs`" (wrong advice for an H-id besides).
        (self.root / 'people' / 'hartley__thomas_P-1111111111.md').write_text(
            '---\nid: P-1111111111\nname: Thomas Hartley\nliving: false\n---\n\n'
            '# Thomas Hartley\n\n## Hypotheses\n\n'
            '- id: H-bqwstmdxb6\n'
            '  hypothesis: "possible duplicate of the unplaced stub"\n'
            '  origin: agent\n  status: open\n', encoding='utf-8')
        (self.root / 'sources').mkdir(exist_ok=True)
        (self.root / 'sources' / 'interview_S-1111111111.md').write_text(
            '---\nid: S-1111111111\ntitle: t\nsource_type: other\n---\n\n'
            'Working theory: [[H-bqwstmdxb6]] covers the identity question.\n',
            encoding='utf-8')
        findings, reg = lint._run_lint_core(self.root, {})
        self.assertIn('h-bqwstmdxb6', reg.hypothesis_ids)
        e004 = [f for f in findings if f.code == 'E004' and 'h-bqwstmdxb6' in f.message]
        self.assertEqual(e004, [], [f.message for f in findings])

    def test_hypothesis_defined_in_a_stub_is_not_e004(self) -> None:
        # #56's worst case: a stub has NO companion files at all (SPEC §16),
        # so its own body is the only legal place a hypothesis about it can
        # live - the old filename gate made that hypothesis permanently
        # uncitable except as plain text (against _STANDARD.md §11).
        (self.root / 'people' / 'stubs').mkdir(parents=True, exist_ok=True)
        (self.root / 'people' / 'stubs' / 'unknown__unknown_P-2222222222.md').write_text(
            '---\nid: P-2222222222\nname: unknown\nliving: unknown\n'
            'tier: stub\n---\n\n## Hypotheses\n\n'
            '- id: H-mtvwstmdx7\n'
            '  hypothesis: "same man as P-1111111111"\n'
            '  origin: agent\n  status: open\n', encoding='utf-8')
        (self.root / 'sources').mkdir(exist_ok=True)
        (self.root / 'sources' / 'interview_S-1111111111.md').write_text(
            '---\nid: S-1111111111\ntitle: t\nsource_type: other\n---\n\n'
            'Working theory: [[H-mtvwstmdx7]] covers the identity question.\n',
            encoding='utf-8')
        findings, reg = lint._run_lint_core(self.root, {})
        self.assertIn('h-mtvwstmdx7', reg.hypothesis_ids)
        e004 = [f for f in findings if f.code == 'E004' and 'h-mtvwstmdx7' in f.message]
        self.assertEqual(e004, [], [f.message for f in findings])


class ResearchCompanionIdentityTests(unittest.TestCase):
    """`research` is a filename SLOT, not a word found anywhere in the stem.

    SPEC §13 puts the companion kind immediately before the P-id
    (`hartley__thomas_research_P-…`), so anywhere else in the name it belongs
    to the given names. A woman recorded as Research Anne Smith
    (`smith__research_anne_P-…`) has an ordinary profile: its `## Hypotheses`
    entries are not SPEC §16 hypothesis records, and its `## Open Questions`
    block is not part of the E009 question scope.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    _BODY = (
        '## Open Questions\n\n'
        '## Q: Which ship did she arrive on?\n'
        '- origin: human\n'
        '- status: open\n\n'
        '## Hypotheses\n\n'
        '- id: H-abcabcabca\n'
        '  hypothesis: "arrived by ~1869"\n'
        '  basis: "railroad boom"\n'
        '  origin: agent\n'
        '  status: open\n'
    )

    def _write(self, filename: str) -> Path:
        """A person record: the SPEC §9 required fields, then the body."""
        path = self.root / 'people' / filename
        path.write_text(
            '---\nid: P-3333333333\nname: Research Anne Smith\nliving: false\n---\n\n'
            + self._BODY, encoding='utf-8')
        return path

    def _write_research(self, filename: str) -> Path:
        """The SPEC §16 research companion, as `fha person promote` scaffolds it.

        An `id:` and a `created:` date and nothing else - which is exactly why
        `id:` can never be part of the person-record test, and why this file
        is a research companion while `_write`'s is a person's own record.
        """
        path = self.root / 'people' / filename
        path.write_text(
            '---\nid: P-3333333333\ncreated: 2026-06-12\n---\n\n' + self._BODY,
            encoding='utf-8')
        return path

    def test_a_given_name_containing_the_kind_word_is_still_a_profile(self) -> None:
        path = self._write('smith__research_anne_P-3333333333.md')
        _findings, reg = lint._run_lint_core(self.root, {})
        # #56: a profile's own ## Hypotheses section is a real record whatever
        # its filename looks like - content decides, so this DOES register
        # despite the file being (correctly) a profile, not a research
        # companion. Open Questions is a different, still-filename-scoped
        # gate (SPEC §16 homes it only in the research companion) and stays
        # empty here.
        self.assertIn('h-abcabcabca', reg.hypothesis_ids)
        self.assertEqual(list(reg.research_content), [])
        # The other half of the same reading: it registers as a profile, so the
        # required-field checks that only profiles get still apply to it.
        self.assertIn('p-3333333333', reg.person_profile_paths)
        self.assertEqual(reg.person_companion_paths.get('p-3333333333', []), [])
        self.assertEqual(reg.person_profile_paths['p-3333333333'], [path])

    def test_the_real_research_companion_is_still_read(self) -> None:
        path = self._write_research('smith__anne_research_P-3333333333.md')
        _findings, reg = lint._run_lint_core(self.root, {})
        self.assertIn('h-abcabcabca', reg.hypothesis_ids)
        self.assertEqual(list(reg.research_content), [path])

    def test_a_person_record_in_that_slot_is_her_record_not_research(self) -> None:
        # The slot before the P-id is also a legal last given name, so this
        # same filename may be Anne Research Smith's own record. A profile is
        # not a research file however it is named - SPEC §16 still homes the
        # Open Questions scope only in the research companion - but #56
        # widened Hypotheses to any person file that carries the section, so
        # her own ## Hypotheses block here IS a real record now.
        self._write('smith__anne_research_P-3333333333.md')
        _findings, reg = lint._run_lint_core(self.root, {})
        self.assertIn('h-abcabcabca', reg.hypothesis_ids)
        self.assertEqual(list(reg.research_content), [])

    def test_an_id_less_research_companion_is_still_read(self) -> None:
        # Mid-graduation: the file carries the kind but no id yet. The kind
        # slot is the last thing in the stem when the id is absent, so the
        # hypotheses it defines still count as existing records for E004.
        path = self.root / 'people' / 'smith__anne_research.md'
        path.write_text(self._BODY, encoding='utf-8')
        _findings, reg = lint._run_lint_core(self.root, {})
        self.assertIn('h-abcabcabca', reg.hypothesis_ids)
        self.assertEqual(list(reg.research_content), [path])


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


class NegatedVitalPolarityTests(unittest.TestCase):
    """A negated claim is a confirmed ABSENCE, never a positive vital: it must
    not satisfy the W101 vitals-gap check nor supersede a provisional date's
    needs-sourcing reminder. The negated-MARRIAGE completeness rule ("never
    married" IS a completeness signal) is the one exception and stays."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _person(self, extra: str) -> None:
        (self.root / 'people' / 'rivera__sam_P-1111111111.md').write_text(
            '---\nid: P-1111111111\nname: Sam Rivera\ntier: curated\n'
            f'living: false\n{extra}---\n\n# Sam Rivera\n', encoding='utf-8')

    def _source(self, claims_yaml: str) -> None:
        (self.root / 'sources' / 'test_S-1111111111.md').write_text(
            _claims_source(claims_yaml), encoding='utf-8')

    def test_negated_birth_does_not_satisfy_w101_vitals_gap(self) -> None:
        self._person('birth:\ndeath:\n')
        self._source(
            '- id: C-1111111111\n  type: birth\n  persons: [P-1111111111]\n'
            '  value: not born 1900\n  status: accepted\n  confidence: high\n'
            '  negated: true\n')
        findings, _ = lint._run_lint_core(self.root, {})
        w101 = [f for f in findings if f.code == 'W101']
        self.assertEqual(len(w101), 1, findings)
        self.assertIn('birth', w101[0].message)

    def test_positive_birth_still_satisfies_w101(self) -> None:
        # Control: a NON-negated accepted birth claim DOES satisfy the gap.
        self._person('birth:\ndeath:\n')
        self._source(
            '- id: C-1111111111\n  type: birth\n  persons: [P-1111111111]\n'
            '  value: born 1900\n  status: accepted\n  confidence: high\n')
        findings, _ = lint._run_lint_core(self.root, {})
        w101 = [f for f in findings if f.code == 'W101']
        self.assertTrue(w101)
        self.assertNotIn('birth', w101[0].message)
        self.assertIn('death', w101[0].message)

    def test_negated_birth_does_not_supersede_provisional_sourcing(self) -> None:
        self._person('birth: 1985~\n')
        self._source(
            '- id: C-1111111111\n  type: birth\n  persons: [P-1111111111]\n'
            '  value: not born 1985\n  status: accepted\n  confidence: high\n'
            '  negated: true\n')
        backlog = lint.run_lint(self.root, {}).data['backlog']
        self.assertTrue([l for l in backlog if "provisional birth: '1985~'" in l], backlog)


class VitalSubjectRoleW101Tests(unittest.TestCase):
    """W101 must only credit a person with their OWN birth/marriage/death when
    a claim's `roles:` map actually casts them as the record's SUBJECT
    (`_lib.vital_subjects`), not merely names them in `persons:` as a
    parent/witness/informant on someone ELSE's vital record. Counting any
    claim that names pid - regardless of role - is the false-negative twin of
    issue #126 (a false life-date): a real vitals gap silently reads as
    covered because the person happens to appear on a relative's record."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_parent_role_on_childs_birth_claim_does_not_satisfy_own_birth(self) -> None:
        # P-1111111111 (the parent) has no birth claim of their own. The only
        # birth claim naming them is their CHILD's (P-2222222222) - and there
        # roles: casts them as `parent`, not the claim's subject. W101 must
        # still report P-1111111111's own birth as missing.
        (self.root / 'people' / 'rivera__sam_P-1111111111.md').write_text(
            '---\nid: P-1111111111\nname: Sam Rivera\ntier: curated\n'
            'living: false\nno_known_marriages: true\n---\n\n# Sam Rivera\n',
            encoding='utf-8')
        (self.root / 'people' / 'rivera__jr_P-2222222222.md').write_text(
            '---\nid: P-2222222222\nname: Sam Rivera Jr\ntier: stub\n'
            'living: false\n---\n\n# Sam Rivera Jr\n', encoding='utf-8')
        (self.root / 'sources' / 'test_S-1111111111.md').write_text(_claims_source(
            '- id: C-1111111111\n  type: birth\n'
            '  persons: [P-2222222222, P-1111111111]\n  roles:\n'
            '    child: [P-2222222222]\n    parent: [P-1111111111]\n'
            '  value: Sam Jr born 1925\n  status: accepted\n  confidence: high\n'
        ), encoding='utf-8')
        findings, _ = lint._run_lint_core(self.root, {})
        w101 = [f for f in findings if f.code == 'W101']
        self.assertEqual(len(w101), 1, findings)
        self.assertIn('birth', w101[0].message)

    def test_two_person_death_claim_with_no_roles_at_all_does_not_satisfy_own_death(
            self) -> None:
        # #126, reopened: a death claim naming P-1111111111 alongside a
        # relative, with NO roles: map at all (not even a partial one), has
        # not said which of them died - `_lib.vital_subjects` now answers []
        # for this shape rather than treating it as "everyone's own vital".
        # W101 must still report P-1111111111's own death as missing rather
        # than crediting it from a claim that may actually be the relative's.
        (self.root / 'people' / 'rivera__sam_P-1111111111.md').write_text(
            '---\nid: P-1111111111\nname: Sam Rivera\ntier: curated\n'
            'living: false\nno_known_marriages: true\n---\n\n# Sam Rivera\n',
            encoding='utf-8')
        (self.root / 'people' / 'rivera__kin_P-3333333333.md').write_text(
            '---\nid: P-3333333333\nname: Kin Rivera\ntier: stub\n'
            'living: false\n---\n\n# Kin Rivera\n', encoding='utf-8')
        (self.root / 'sources' / 'test_S-3333333333.md').write_text(_claims_source(
            '- id: C-3333333333\n  type: death\n'
            '  persons: [P-1111111111, P-3333333333]\n'
            '  value: Visited the grave in 1990\n  status: accepted\n'
            '  confidence: high\n'
        ), encoding='utf-8')
        findings, _ = lint._run_lint_core(self.root, {})
        w101 = [f for f in findings if f.code == 'W101']
        self.assertEqual(len(w101), 1, findings)
        self.assertIn('death', w101[0].message)


class VitalSubjectScopedBacklogTests(unittest.TestCase):
    """`_accepted_vital_pids` (the needs-sourcing backlog's "already superseded"
    set) must scope an accepted vital claim through `_lib.vital_subjects`
    exactly like W101 already does, not the old unscoped `_claim_person_ids`
    membership test. Two false shapes this covers: a claim naming pid only as
    a relative on someone ELSE's birth/death record, and (#126 reopened) a
    claim naming 2+ people with no `roles:` map at all - `vital_subjects`
    answers [] for that shape, not "everyone". Crediting either one silently
    drops a real provisional date from the backlog over a claim that may not
    be this person's at all (#136)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_zero_role_multiperson_death_does_not_supersede_the_backlog(self) -> None:
        # P-1111111111 has a provisional death: date recorded but not yet
        # sourced. The only accepted death claim naming them also names a
        # relative, with NO roles: map at all - #126 reopened's shape, where
        # `vital_subjects` answers [] rather than "everyone". The backlog must
        # still nudge for a source rather than reading this claim as it.
        (self.root / 'people' / 'rivera__sam_P-1111111111.md').write_text(
            '---\nid: P-1111111111\nname: Sam Rivera\nliving: false\n'
            'death: 1941~\n---\n\n# Sam Rivera\n', encoding='utf-8')
        (self.root / 'people' / 'rivera__kin_P-3333333333.md').write_text(
            '---\nid: P-3333333333\nname: Kin Rivera\nliving: false\n'
            '---\n\n# Kin Rivera\n', encoding='utf-8')
        (self.root / 'sources' / 'test_S-3333333333.md').write_text(_claims_source(
            '- id: C-3333333333\n  type: death\n'
            '  persons: [P-1111111111, P-3333333333]\n'
            '  value: Visited the grave in 1990\n  status: accepted\n'
            '  confidence: high\n'
        ), encoding='utf-8')
        backlog = lint.run_lint(self.root, {}).data['backlog']
        self.assertTrue(
            [l for l in backlog if "provisional death: '1941~'" in l], backlog)

    def test_single_subject_death_still_supersedes_the_backlog(self) -> None:
        # Control: the legacy single-person shape (`vital_subjects` case 2,
        # answers None - "keep the old behaviour") still supersedes, exactly
        # as it did before this fix.
        (self.root / 'people' / 'rivera__sam_P-1111111111.md').write_text(
            '---\nid: P-1111111111\nname: Sam Rivera\nliving: false\n'
            'death: 1941~\n---\n\n# Sam Rivera\n', encoding='utf-8')
        (self.root / 'sources' / 'test_S-1111111111.md').write_text(_claims_source(
            '- id: C-1111111111\n  type: death\n'
            '  persons: [P-1111111111]\n'
            '  value: died 1941\n  status: accepted\n'
            '  confidence: high\n'
        ), encoding='utf-8')
        backlog = lint.run_lint(self.root, {}).data['backlog']
        self.assertFalse(
            [l for l in backlog if "provisional death: '1941~'" in l], backlog)

    def test_parent_role_on_childs_birth_claim_does_not_supersede_the_backlog(self) -> None:
        # The role-scoped twin: P-1111111111 only appears as `parent` on their
        # CHILD's birth claim - not their own subject (the false-negative
        # twin of #126, #136).
        (self.root / 'people' / 'rivera__sam_P-1111111111.md').write_text(
            '---\nid: P-1111111111\nname: Sam Rivera\nliving: false\n'
            'birth: 1900~\n---\n\n# Sam Rivera\n', encoding='utf-8')
        (self.root / 'people' / 'rivera__jr_P-2222222222.md').write_text(
            '---\nid: P-2222222222\nname: Sam Rivera Jr\nliving: false\n'
            '---\n\n# Sam Rivera Jr\n', encoding='utf-8')
        (self.root / 'sources' / 'test_S-1111111111.md').write_text(_claims_source(
            '- id: C-1111111111\n  type: birth\n'
            '  persons: [P-2222222222, P-1111111111]\n  roles:\n'
            '    child: [P-2222222222]\n    parent: [P-1111111111]\n'
            '  value: Sam Jr born 1925\n  status: accepted\n  confidence: high\n'
        ), encoding='utf-8')
        backlog = lint.run_lint(self.root, {}).data['backlog']
        self.assertTrue(
            [l for l in backlog if "provisional birth: '1900~'" in l], backlog)


class VitalSubjectScopedSummaryCitationW104Tests(unittest.TestCase):
    """W104: a profile's own Born/Died/Married summary line must be backed by
    a claim `_lib.vital_subjects` actually resolves to THAT profile person -
    not merely a same-source, same-type claim that happens to name them
    somewhere (#126 reopened, #136). Parents/Children are untouched (E013's
    own parentage-scoped check already covers that line)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _profile(self, pid: str, name: str, summary: str) -> None:
        (self.root / 'people' / f'x__{pid}.md').write_text(
            f'---\nid: {pid}\nname: {name}\ntier: curated\nliving: false\n'
            f'---\n\n# {name}\n\n{summary}\n\n## Biography\n\nx\n',
            encoding='utf-8')

    def test_zero_role_multiperson_death_does_not_back_died_summary(self) -> None:
        self._profile('P-1111111111', 'Sam Rivera', '**Died:** 1941 [S-1111111111]')
        (self.root / 'people' / 'x__P-3333333333.md').write_text(
            '---\nid: P-3333333333\nname: Kin Rivera\nliving: false\n---\n\n'
            '# Kin Rivera\n', encoding='utf-8')
        (self.root / 'sources' / 'test_S-1111111111.md').write_text(_claims_source(
            '- id: C-1111111111\n  type: death\n'
            '  persons: [P-1111111111, P-3333333333]\n'
            '  value: Visited the grave\n  status: accepted\n'
            '  confidence: high\n'
        ), encoding='utf-8')
        findings, _ = lint._run_lint_core(self.root, {})
        w104 = [f for f in findings if f.code == 'W104']
        self.assertEqual(len(w104), 1, findings)

    def test_single_subject_death_still_backs_died_summary(self) -> None:
        self._profile('P-1111111111', 'Sam Rivera', '**Died:** 1941 [S-1111111111]')
        (self.root / 'sources' / 'test_S-1111111111.md').write_text(_claims_source(
            '- id: C-1111111111\n  type: death\n'
            '  persons: [P-1111111111]\n'
            '  value: died 1941\n  status: accepted\n'
            '  confidence: high\n'
        ), encoding='utf-8')
        findings, _ = lint._run_lint_core(self.root, {})
        w104 = [f for f in findings if f.code == 'W104']
        self.assertEqual(w104, [], findings)

    def test_role_scoped_death_naming_someone_else_does_not_back_summary(self) -> None:
        # roles: explicitly casts P-1111111111 as the surviving spouse, not
        # the deceased - `vital_subjects` names the OTHER person instead.
        self._profile('P-1111111111', 'Sam Rivera', '**Died:** 1941 [S-1111111111]')
        (self.root / 'people' / 'x__P-3333333333.md').write_text(
            '---\nid: P-3333333333\nname: Kin Rivera\nliving: false\n---\n\n'
            '# Kin Rivera\n', encoding='utf-8')
        (self.root / 'sources' / 'test_S-1111111111.md').write_text(_claims_source(
            '- id: C-1111111111\n  type: death\n'
            '  persons: [P-3333333333, P-1111111111]\n  roles:\n'
            '    spouse: [P-1111111111]\n'
            '  value: Kin Rivera died 1941, survived by his wife Sam\n'
            '  status: accepted\n  confidence: high\n'
        ), encoding='utf-8')
        findings, _ = lint._run_lint_core(self.root, {})
        w104 = [f for f in findings if f.code == 'W104']
        self.assertEqual(len(w104), 1, findings)


class _SurgeryBase(unittest.TestCase):
    """Shared scaffolding for the fix-mode surgery tests: one named person and
    one source file whose bytes the test controls exactly (write_bytes, so
    line endings are what the fixture says, not what the platform prefers)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir()
        (self.root / 'people' / 'rivera__sam_P-1111111111.md').write_text(
            _NAMED_PERSON, encoding='utf-8')
        self.src = self.root / 'sources' / 'test_S-1111111111.md'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_src(self, text: str) -> None:
        self.src.write_bytes(text.encode('utf-8'))

    def _progress(self, result) -> str:
        return '\n'.join(result.data['progress'])


class ClaimMintSurgeryGuardTests(_SurgeryBase):
    """--fix-ids claim surgery is GUARDED: the four failure modes that used to
    corrupt files (second blank id: key, reviewed: stamped into a value: |
    scalar, wholesale newline rewrite, anchor-led item broken) now either fix
    correctly or refuse plainly, and the whole rewrite is re-parsed before any
    write so the success message can never lie."""

    def _fenced(self, claims_yaml: str) -> str:
        return ('---\nid: S-1111111111\ntitle: t\nsource_type: other\n---\n\n'
                '## Claims\n\n```yaml\n' + claims_yaml + '```\n')

    def test_blank_id_line_is_completed_in_place(self) -> None:
        # Mode (a): `id:` with no value used to get a SECOND id: key inserted;
        # YAML keeps the last (blank) one, so the mint was void, "Minted 1
        # claim id(s)" lied, and every rerun burned another id.
        self._write_src(self._fenced(
            '- id:\n  value: born 1985\n  type: birth\n'
            '  persons: [P-1111111111]\n  status: suggested\n  confidence: low\n'))
        result = lint.run_lint(self.root, {}, fix_ids=True)
        self.assertIn('Minted 1 claim id(s)', self._progress(result))
        text = self.src.read_text(encoding='utf-8')
        block = CLAIMS_RE.search(text).group(1)
        id_lines = [l for l in block.splitlines() if re.match(r'\s*(?:-\s+)?id:', l)]
        self.assertEqual(len(id_lines), 1, block)   # completed IN PLACE, no second key
        claim = read_record(self.src)['claims'][0]
        self.assertTrue(str(claim.get('id', '')).lower().startswith('c-'), claim)
        # Idempotent: a second run finds nothing left to mint.
        before = self.src.read_bytes()
        lint.run_lint(self.root, {}, fix_ids=True)
        self.assertEqual(self.src.read_bytes(), before)

    def test_blank_id_line_keeps_its_comment(self) -> None:
        self._write_src(self._fenced(
            '- id:   # a tool can fill this\n  value: born 1985\n  type: birth\n'
            '  persons: [P-1111111111]\n  status: suggested\n  confidence: low\n'))
        lint.run_lint(self.root, {}, fix_ids=True)
        text = self.src.read_text(encoding='utf-8')
        claim = read_record(self.src)['claims'][0]
        cid = str(claim['id'])
        self.assertTrue(cid.lower().startswith('c-'))
        self.assertRegex(text, rf'id: {cid}\s+# a tool can fill this')

    def test_reviewed_stamp_skips_scalar_lookalike(self) -> None:
        # Mode (b): a `status: accepted` line QUOTED inside value: | used to
        # receive the reviewed: stamp (mutating the human's evidence) while
        # the real claim stayed unstamped and E006 persisted.
        evidence = 'the letter says:\nstatus: accepted\n'
        self._write_src(self._fenced(
            '- value: |\n    the letter says:\n    status: accepted\n'
            '  type: note\n  persons: [P-1111111111]\n'
            '  status: accepted\n  confidence: low\n'))
        lint.run_lint(self.root, {}, fix_ids=True)
        claim = read_record(self.src)['claims'][0]
        self.assertEqual(claim['value'], evidence)   # evidence byte-identical
        self.assertEqual(str(claim.get('reviewed', '')),
                         datetime.date.today().isoformat())
        text = self.src.read_text(encoding='utf-8')
        # Exactly one reviewed: line, at the claim's own key column (2 spaces).
        self.assertEqual(len(re.findall(r'^  reviewed:', text, re.M)), 1, text)
        self.assertNotRegex(text, r'(?m)^    reviewed:')

    def test_lf_file_stays_lf_outside_the_edits(self) -> None:
        # Mode (c): Path.write_text turned a whole LF archive CRLF on Windows;
        # the surgery contract is byte-preserving outside the edited spans.
        src_text = self._fenced(
            '- value: born 1985\n  type: birth\n  persons: [P-1111111111]\n'
            '  status: suggested\n  confidence: low\n')
        self._write_src(src_text)
        self.assertNotIn(b'\r', self.src.read_bytes())
        lint.run_lint(self.root, {}, fix_ids=True)
        after = self.src.read_bytes()
        self.assertNotIn(b'\r', after)
        # Every original line survives byte-for-byte except the dash line,
        # whose first field moved down one line with its bytes untouched.
        after_lines = after.decode('utf-8').splitlines()
        for line in src_text.splitlines():
            if line.startswith('- '):
                self.assertIn('  ' + line[2:], after_lines, line)
            else:
                self.assertIn(line, after_lines, line)

    def test_anchor_led_item_is_refused_not_broken(self) -> None:
        # Mode (d): inserting id: above a `- &c1` anchor detached the anchor
        # and the WHOLE block stopped parsing - every claim in the source
        # vanished under a success message.
        self._write_src(self._fenced(
            '- &c1\n  value: born 1985\n  type: birth\n'
            '  persons: [P-1111111111]\n  status: suggested\n  confidence: low\n'))
        before = self.src.read_bytes()
        result = lint.run_lint(self.root, {}, fix_ids=True)
        self.assertEqual(self.src.read_bytes(), before)
        progress = self._progress(result)
        self.assertIn('anchor', progress)
        self.assertIn('fha id mint C', progress)
        self.assertNotIn('Minted', progress)
        rec = read_record(self.src)
        self.assertEqual(rec['parse_errors'], [])   # block still parses
        self.assertEqual(len(rec['claims']), 1)

    def test_bad_rewrite_is_refused_and_file_untouched(self) -> None:
        # The write guard end to end: force the rewrite to be garbage (a
        # minted "id" that breaks YAML) and prove refusal, not corruption.
        self._write_src(self._fenced(
            '- value: born 1985\n  type: birth\n  persons: [P-1111111111]\n'
            '  status: suggested\n  confidence: low\n'))
        before = self.src.read_bytes()
        real_mint = lint.mint_ids
        lint.mint_ids = lambda kind, count, root: ['C-1111111111\nGARBAGE: ['] * count
        try:
            result = lint.run_lint(self.root, {}, fix_ids=True)
        finally:
            lint.mint_ids = real_mint
        self.assertEqual(self.src.read_bytes(), before)
        progress = self._progress(result)
        self.assertIn('stopped before writing', progress)
        self.assertIn('fha id mint C', progress)

    def test_duplicate_blank_id_keys_are_refused(self) -> None:
        # Two blank id: keys in one item: completing the first still leaves
        # YAML keeping the last (blank) one - the parse-back count catches it.
        self._write_src(self._fenced(
            '- id:\n  value: born 1985\n  id:\n  type: birth\n'
            '  persons: [P-1111111111]\n  status: suggested\n  confidence: low\n'))
        before = self.src.read_bytes()
        result = lint.run_lint(self.root, {}, fix_ids=True)
        self.assertEqual(self.src.read_bytes(), before)
        self.assertIn('stopped before writing', self._progress(result))

    def test_unfenced_claims_are_sequenced_to_the_fence_fixer(self) -> None:
        # --fix-ids alone no longer operates on the W114 unfenced form (the
        # write guard vets the fenced form); it names the sequence instead,
        # and a combined run still completes the graduation in one pass.
        self._write_src(
            '---\nid: S-1111111111\ntitle: t\nsource_type: other\n---\n\n'
            '## Claims\n- value: born 1985\n  type: birth\n'
            '  persons: [P-1111111111]\n  status: suggested\n  confidence: low\n')
        before = self.src.read_bytes()
        result = lint.run_lint(self.root, {}, fix_ids=True)
        self.assertEqual(self.src.read_bytes(), before)
        self.assertIn('--fix-claims-fence', self._progress(result))
        combined = lint.run_lint(self.root, {}, fix_claims_fence=True, fix_ids=True)
        self.assertIn('Minted 1 claim id(s)', self._progress(combined))
        claim = read_record(self.src)['claims'][0]
        self.assertTrue(str(claim.get('id', '')).lower().startswith('c-'), claim)


class ClaimsFenceFixTests(_SurgeryBase):
    """--fix-claims-fence must produce a fence that re-reads to the SAME
    claims the unfenced reader parsed, and must never delete fence-lookalike
    ``` lines from a claim's quoted evidence - it refuses instead."""

    def _unfenced(self, claims_section: str) -> str:
        return ('---\nid: S-1111111111\ntitle: t\nsource_type: other\n---\n\n'
                '## Claims\n' + claims_section)

    def test_indented_first_item_round_trips(self) -> None:
        # A tab-indented item: the unfenced reader dedents (join + strip) and
        # parses one claim; the old fixer fenced the RAW text, whose tab is
        # invalid YAML - n_claims went 1 -> 0 right after the W114 message
        # told the human to run exactly this fix.
        self._write_src(self._unfenced(
            '\t- {value: farmer, type: note, persons: [P-1111111111], '
            'status: suggested, confidence: low}\n'))
        self.assertEqual(len(read_record(self.src)['claims']), 1)
        result = lint.run_lint(self.root, {}, fix_claims_fence=True)
        self.assertIn(str(self.src), result.changed)
        rec = read_record(self.src)
        self.assertEqual(rec['parse_errors'], [])
        self.assertEqual(len(rec['claims']), 1)
        self.assertEqual(rec['claims'][0]['value'], 'farmer')
        self.assertFalse(rec['unfenced_claims'])

    def test_lookalike_fence_line_is_refused_evidence_intact(self) -> None:
        # ``` lines inside a value: | scalar are the human's quoted evidence.
        # The old fixer silently DELETED them from disk; now the file is
        # refused with the line number and left byte-identical.
        self._write_src(self._unfenced(
            '- value: |\n    he wrote:\n    ```\n    code sample\n    ```\n'
            '  status: suggested\n- value: plain\n  status: suggested\n'))
        before = self.src.read_bytes()
        result = lint.run_lint(self.root, {}, fix_claims_fence=True)
        self.assertEqual(self.src.read_bytes(), before)
        self.assertEqual(result.changed, [])
        refusals = [l for l in result.data['progress'] if '```' in l]
        self.assertTrue(refusals, result.data['progress'])
        self.assertIn(self.src.name, refusals[0])
        self.assertIn('line 10', refusals[0])   # the first ``` line of the file
        self.assertIn('by hand', refusals[0])
        self.assertEqual(len(read_record(self.src)['claims']), 2)

    def test_fence_dry_run_previews_and_writes_nothing(self) -> None:
        self._write_src(self._unfenced(
            '- value: farmer\n  type: note\n  persons: [P-1111111111]\n'
            '  status: suggested\n  confidence: low\n'))
        before = self.src.read_bytes()
        result = lint.run_lint(self.root, {}, fix_claims_fence=True, dry_run=True)
        self.assertEqual(self.src.read_bytes(), before)
        self.assertEqual(result.changed, [])
        self.assertIn('would wrap', self._progress(result))

    def test_opening_only_missing_close_is_repaired(self) -> None:
        # #52: a hand-edit deleted the closing ``` and left the opening
        # ```yaml in place. The OLD fixer saw that surviving ```yaml line as
        # a lookalike quoted inside a value and refused the whole file,
        # naming --fix-claims-fence as the remedy while doing nothing - the
        # exact bug reported. The repair must insert the missing delimiter.
        self._write_src(self._unfenced(
            '```yaml\n- value: farmer\n  type: note\n  persons: [P-1111111111]\n'
            '  status: suggested\n  confidence: low\n'))
        self.assertEqual(len(read_record(self.src)['claims']), 1)
        result = lint.run_lint(self.root, {}, fix_claims_fence=True)
        self.assertIn(str(self.src), result.changed)
        rec = read_record(self.src)
        self.assertEqual(rec['parse_errors'], [])
        self.assertEqual(len(rec['claims']), 1)
        self.assertEqual(rec['claims'][0]['value'], 'farmer')
        self.assertFalse(rec['unfenced_claims'])
        findings, _ = lint._run_lint_core(self.root, {})
        self.assertEqual([f for f in findings if f.code == 'W114'], [])

    def test_closing_only_missing_open_is_repaired(self) -> None:
        # #52's other asymmetric case: the opening ```yaml was never typed
        # (or was deleted) but a stray closing ``` remains at the end of the
        # section.
        self._write_src(self._unfenced(
            '- value: farmer\n  type: note\n  persons: [P-1111111111]\n'
            '  status: suggested\n  confidence: low\n```\n'))
        self.assertEqual(len(read_record(self.src)['claims']), 1)
        result = lint.run_lint(self.root, {}, fix_claims_fence=True)
        self.assertIn(str(self.src), result.changed)
        rec = read_record(self.src)
        self.assertEqual(rec['parse_errors'], [])
        self.assertEqual(len(rec['claims']), 1)
        self.assertEqual(rec['claims'][0]['value'], 'farmer')
        self.assertFalse(rec['unfenced_claims'])
        findings, _ = lint._run_lint_core(self.root, {})
        self.assertEqual([f for f in findings if f.code == 'W114'], [])

    def test_no_fence_at_all_still_leaves_lint_clean(self) -> None:
        # Pre-existing fixture shape (the fully unfenced case) - a fourth
        # shape alongside the two asymmetric ones above, kept green here so
        # the #52 fix cannot be verified to work on the asymmetric cases
        # while silently regressing the case that already worked.
        self._write_src(self._unfenced(
            '- value: farmer\n  type: note\n  persons: [P-1111111111]\n'
            '  status: suggested\n  confidence: low\n'))
        result = lint.run_lint(self.root, {}, fix_claims_fence=True)
        self.assertIn(str(self.src), result.changed)
        findings, _ = lint._run_lint_core(self.root, {})
        self.assertEqual([f for f in findings if f.code == 'W114'], [])

    def test_bare_fence_both_ends_no_yaml_tag_is_repaired(self) -> None:
        # A fifth shape the #52 fix itself introduced a new way to get wrong:
        # a hand-typed ``` on BOTH the opening and closing line, with no
        # `yaml` language tag on either. CLAIMS_RE requires the literal
        # ```yaml opener, so this never reads as fenced and W114 fires - but
        # the boundary scan (language tag optional on both ends) recognises
        # BOTH lines as markers, which the first version of the #52 fix
        # treated as proof the file must already be fenced (an assumed-
        # unreachable case) and silently returned "nothing to wrap" for -
        # W114 kept firing forever with --fix-claims-fence reporting nothing
        # at all, the exact silent-refusal failure #52 was filed over.
        self._write_src(self._unfenced(
            '```\n- value: farmer\n  type: note\n  persons: [P-1111111111]\n'
            '  status: suggested\n  confidence: low\n```\n'))
        self.assertEqual(len(read_record(self.src)['claims']), 1)
        result = lint.run_lint(self.root, {}, fix_claims_fence=True)
        self.assertIn(str(self.src), result.changed)
        rec = read_record(self.src)
        self.assertEqual(rec['parse_errors'], [])
        self.assertEqual(len(rec['claims']), 1)
        self.assertEqual(rec['claims'][0]['value'], 'farmer')
        self.assertFalse(rec['unfenced_claims'])
        findings, _ = lint._run_lint_core(self.root, {})
        self.assertEqual([f for f in findings if f.code == 'W114'], [])


class NearMissIdTests(_SurgeryBase):
    """A claim reference that LOOKS like a mistyped record code must produce a
    finding again (E005 for persons, E004 for corroborates/contradicts) - the
    alias-resolution tolerance had made typo'd codes silently inert, so the
    claim detached from its person with no message anywhere. Genuine names
    keep the TOOLING contract: resolvable is fine, unresolvable stays an
    inert note-link."""

    def _lint_claims(self, claims_yaml: str) -> list:
        self._write_src(
            '---\nid: S-1111111111\ntitle: t\nsource_type: other\n---\n\n'
            '## Claims\n\n```yaml\n' + claims_yaml + '```\n')
        findings, _ = lint._run_lint_core(self.root, {})
        return findings

    def test_nine_char_person_code_is_e005(self) -> None:
        findings = self._lint_claims(
            '- id: C-1111111111\n  type: birth\n  persons: [P-de957bcda]\n'
            '  value: x\n  status: suggested\n  confidence: low\n')
        hits = [f for f in findings if f.code == 'E005' and 'P-de957bcda' in f.message]
        self.assertEqual(len(hits), 1, [f.message for f in findings])
        msg = hits[0].message
        self.assertIn('looks like a person code', msg)
        self.assertIn('9 character(s)', msg)
        self.assertIn('i l o u', msg)          # the alphabet gloss, in plain words
        self.assertIn("person's name", msg)    # the recovery path

    def test_bad_letter_person_code_is_e005(self) -> None:
        findings = self._lint_claims(
            '- id: C-1111111111\n  type: birth\n  persons: [P-de957bcdal]\n'
            '  value: x\n  status: suggested\n  confidence: low\n')
        hits = [f for f in findings if f.code == 'E005' and 'P-de957bcdal' in f.message]
        self.assertEqual(len(hits), 1)
        self.assertIn("'l'", hits[0].message)  # names the offending letter
        self.assertIn('i l o u', hits[0].message)

    def test_truncated_corroborates_target_is_e004(self) -> None:
        findings = self._lint_claims(
            '- id: C-1111111111\n  type: birth\n  persons: [P-1111111111]\n'
            '  value: x\n  status: suggested\n  confidence: low\n'
            '- id: C-2222222222\n  type: birth\n  persons: [P-1111111111]\n'
            '  value: y\n  status: suggested\n  confidence: low\n'
            '  corroborates: [C-de957bcda]\n')
        hits = [f for f in findings if f.code == 'E004' and 'C-de957bcda' in f.message]
        self.assertEqual(len(hits), 1, [f.message for f in findings])
        self.assertIn('looks like a claim code', hits[0].message)
        self.assertIn('exactly 10', hits[0].message)

    def test_resolvable_name_stays_silent(self) -> None:
        findings = self._lint_claims(
            '- id: C-1111111111\n  type: birth\n  persons: ["[[Sam Rivera]]"]\n'
            '  value: x\n  status: suggested\n  confidence: low\n')
        self.assertEqual([f for f in findings if f.code in ('E004', 'E005')], [])

    def test_unresolvable_plain_name_stays_inert(self) -> None:
        findings = self._lint_claims(
            '- id: C-1111111111\n  type: birth\n  persons: ["Ghost Writer"]\n'
            '  value: x\n  status: suggested\n  confidence: low\n')
        self.assertEqual([f for f in findings if f.code in ('E004', 'E005')], [])

    def test_name_plus_year_token_is_not_a_near_miss(self) -> None:
        # `Anna1850` (a name+year, only 8 chars) once tripped the bare-code net
        # because every letter happens to be Crockford; a real bare code is
        # exactly 10 chars, so a shorter alnum name must stay an inert note-link.
        for name in ('Anna1850', 'John1042'):
            with self.subTest(name=name):
                findings = self._lint_claims(
                    '- id: C-1111111111\n  type: birth\n  persons: ["' + name + '"]\n'
                    '  value: x\n  status: suggested\n  confidence: low\n')
                self.assertEqual([f for f in findings if f.code in ('E004', 'E005')], [],
                                 [f.message for f in findings])

    def test_letter_hyphen_name_is_not_a_near_miss(self) -> None:
        # A plain note-link that merely starts with a type letter + hyphen
        # (`L-something`, `C-note`) is not a mistyped code - a code body is
        # code-length, all-alnum, and carries a digit; these don't.
        for name in ('L-something', 'C-note'):
            with self.subTest(name=name):
                findings = self._lint_claims(
                    '- id: C-1111111111\n  type: birth\n  persons: ["' + name + '"]\n'
                    '  value: x\n  status: suggested\n  confidence: low\n')
                self.assertEqual([f for f in findings if f.code in ('E004', 'E005')], [],
                                 [f.message for f in findings])

    def test_prefixless_bare_code_is_flagged(self) -> None:
        findings = self._lint_claims(
            '- id: C-1111111111\n  type: birth\n  persons: [de957bcda1]\n'
            '  value: x\n  status: suggested\n  confidence: low\n')
        hits = [f for f in findings if f.code == 'E005' and 'de957bcda1' in f.message]
        self.assertEqual(len(hits), 1)
        self.assertIn('missing its type prefix', hits[0].message)

    def test_template_placeholder_target_stays_out_of_the_net(self) -> None:
        # `C-__________` is the template's teaching form - its story belongs
        # to E010/--fix-ids, never the typo net.
        findings = self._lint_claims(
            '- id: C-1111111111\n  type: birth\n  persons: [P-1111111111]\n'
            '  value: x\n  status: suggested\n  confidence: low\n'
            '  corroborates: [C-__________]\n')
        self.assertEqual([f for f in findings if f.code == 'E004'], [])

    def test_persons_resolving_to_nobody_warns_w118(self) -> None:
        # A claim whose persons: names only unresolved people attaches to no
        # one - the exact silent-detach gap. Warn (never block), so the likely
        # typo/rename is visible instead of the claim vanishing from timelines.
        findings = self._lint_claims(
            '- id: C-1111111111\n  type: birth\n  persons: ["[[Jon Smith]]"]\n'
            '  value: x\n  status: suggested\n  confidence: low\n')
        w = [f for f in findings if f.code == 'W118']
        self.assertEqual(len(w), 1, [f.message for f in findings])
        self.assertIn('attaches to no one', w[0].message)
        self.assertEqual([f for f in findings if f.code in ('E004', 'E005')], [])

    def test_persons_that_resolve_do_not_warn_w118(self) -> None:
        # The seeded person P-1111111111 exists, so a claim naming them
        # attaches - no detachment warning.
        findings = self._lint_claims(
            '- id: C-1111111111\n  type: birth\n  persons: [P-1111111111]\n'
            '  value: x\n  status: suggested\n  confidence: low\n')
        self.assertEqual([f for f in findings if f.code == 'W118'], [])

    def test_near_miss_code_is_e005_not_also_w118(self) -> None:
        # A near-miss code is already an E005; W118 must not double-report the
        # same broken reference.
        findings = self._lint_claims(
            '- id: C-1111111111\n  type: birth\n  persons: [de957bcda1]\n'
            '  value: x\n  status: suggested\n  confidence: low\n')
        self.assertTrue([f for f in findings if f.code == 'E005'])
        self.assertEqual([f for f in findings if f.code == 'W118'], [])

    def test_partial_resolution_does_not_warn_w118(self) -> None:
        # One resolvable + one unresolved: the claim still attaches (to the
        # seeded person), so the unresolved name stays an inert note-link with
        # no warning - W118 fires only when the WHOLE list detaches.
        findings = self._lint_claims(
            '- id: C-1111111111\n  type: birth\n  persons: [P-1111111111, "Ghosty"]\n'
            '  value: x\n  status: suggested\n  confidence: low\n')
        self.assertEqual([f for f in findings if f.code == 'W118'], [])


_TEMPLATE_COPY_PERSON = '''---
id: P-__________   # OPTIONAL - LINT WILL CREATE FOR YOU LATER IF MISSING
aliases:           # OPTIONAL - the code, repeated
  - P-__________   # paste the same code here too
name: Grandpa Bob
living: false
---

# Grandpa Bob
'''


class AliasMergeTests(unittest.TestCase):
    """--fix-ids on a template copy: templates SHIP an aliases: block, so the
    old `if not has_aliases: skip` dropped the slug and verbatim-stem aliases
    on exactly the files the templates produce - every [[old name]] link died
    on the rename while "(old name kept as an alias)" printed anyway."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_template_copy_merges_slug_and_verbatim_stem(self) -> None:
        (self.root / 'people' / 'Grandpa Bob.md').write_text(
            _TEMPLATE_COPY_PERSON, encoding='utf-8')
        result = lint.run_lint(self.root, {}, fix_ids=True)
        minted = list((self.root / 'people').glob('bob__grandpa_P-*.md'))
        self.assertEqual(len(minted), 1)
        aliases = read_record(minted[0])['meta'].get('aliases') or []
        self.assertIn('grandpa-bob', aliases)
        self.assertIn('Grandpa Bob', aliases)          # the verbatim stem
        text = minted[0].read_text(encoding='utf-8')
        self.assertIn('# paste the same code here too', text)   # block formatting kept
        minted_line = [l for l in result.data['progress'] if l.startswith('Minted')][0]
        self.assertIn('(old name kept as an alias)', minted_line)   # and it is TRUE

    def test_aliases_already_present_means_no_alias_claim_in_message(self) -> None:
        (self.root / 'people' / 'Grandpa Bob.md').write_text(
            _TEMPLATE_COPY_PERSON.replace(
                '  - P-__________   # paste the same code here too',
                '  - P-__________   # paste the same code here too\n'
                '  - grandpa-bob\n'
                '  - "Grandpa Bob"'), encoding='utf-8')
        result = lint.run_lint(self.root, {}, fix_ids=True)
        minted = list((self.root / 'people').glob('bob__grandpa_P-*.md'))
        self.assertEqual(len(minted), 1)
        aliases = read_record(minted[0])['meta'].get('aliases') or []
        self.assertEqual(aliases.count('grandpa-bob'), 1)   # no duplicates minted
        self.assertEqual(aliases.count('Grandpa Bob'), 1)
        minted_line = [l for l in result.data['progress'] if l.startswith('Minted')][0]
        self.assertNotIn('old name kept as an alias', minted_line)  # nothing was added

    def test_no_aliases_block_control_unchanged(self) -> None:
        (self.root / 'people' / 'Grandpa Bob.md').write_text(
            '---\nname: Grandpa Bob\nliving: false\n---\n\n# Grandpa Bob\n',
            encoding='utf-8')
        result = lint.run_lint(self.root, {}, fix_ids=True)
        minted = list((self.root / 'people').glob('bob__grandpa_P-*.md'))
        self.assertEqual(len(minted), 1)
        aliases = read_record(minted[0])['meta'].get('aliases') or []
        self.assertIn('grandpa-bob', aliases)
        self.assertIn('Grandpa Bob', aliases)
        minted_line = [l for l in result.data['progress'] if l.startswith('Minted')][0]
        self.assertIn('(old name kept as an alias)', minted_line)

    def test_flow_form_aliases_block_is_merged(self) -> None:
        (self.root / 'people' / 'Grandpa Bob.md').write_text(
            '---\nid: P-__________\naliases: [P-__________]\n'
            'name: Grandpa Bob\nliving: false\n---\n\n# Grandpa Bob\n',
            encoding='utf-8')
        lint.run_lint(self.root, {}, fix_ids=True)
        minted = list((self.root / 'people').glob('bob__grandpa_P-*.md'))
        self.assertEqual(len(minted), 1)
        aliases = read_record(minted[0])['meta'].get('aliases') or []
        self.assertIn('grandpa-bob', aliases)
        self.assertIn('Grandpa Bob', aliases)

    def test_multi_item_block_appends_after_the_last_item(self) -> None:
        (self.root / 'people' / 'Grandpa Bob.md').write_text(
            _TEMPLATE_COPY_PERSON.replace(
                '  - P-__________   # paste the same code here too',
                '  - P-__________   # paste the same code here too\n  - Bobby'),
            encoding='utf-8')
        lint.run_lint(self.root, {}, fix_ids=True)
        minted = list((self.root / 'people').glob('bob__grandpa_P-*.md'))
        self.assertEqual(len(minted), 1)
        aliases = read_record(minted[0])['meta'].get('aliases') or []
        # New entries land AFTER the existing hand entries, keeping their order.
        self.assertEqual(aliases[1:], ['Bobby', 'grandpa-bob', 'Grandpa Bob'])

    def test_zero_indent_block_list_stays_valid_yaml(self) -> None:
        # A hand-authored zero-indent block list (`aliases:\n- old`) once got a
        # two-space item inserted ahead of the zero-indent ones - mixed-indent
        # YAML that no longer parses. The result must still be a valid mapping.
        (self.root / 'people' / 'Grandpa Bob.md').write_text(
            '---\nname: Grandpa Bob\nliving: false\naliases:\n- old bob\n---\n\n# Grandpa Bob\n',
            encoding='utf-8')
        lint.run_lint(self.root, {}, fix_ids=True)
        minted = list((self.root / 'people').glob('bob__grandpa_P-*.md'))
        self.assertEqual(len(minted), 1)
        meta = read_record(minted[0])['meta']   # read_record parses the YAML - a
        aliases = meta.get('aliases') or []      # corrupt block would raise/empty
        self.assertIn('old bob', aliases)
        self.assertIn('grandpa-bob', aliases)

    def test_flow_list_with_embedded_wikilink_is_preserved(self) -> None:
        # `aliases: [P-x, "[[Old Name]]"]` - the splice must land before the
        # list's real closing bracket, not the `]` inside `]]`, so the existing
        # wikilink alias survives and the new entry is a distinct element.
        (self.root / 'people' / 'Grandpa Bob.md').write_text(
            '---\nid: P-__________\naliases: [P-__________, "[[Old Name]]"]\n'
            'name: Grandpa Bob\nliving: false\n---\n\n# Grandpa Bob\n',
            encoding='utf-8')
        lint.run_lint(self.root, {}, fix_ids=True)
        minted = list((self.root / 'people').glob('bob__grandpa_P-*.md'))
        self.assertEqual(len(minted), 1)
        aliases = read_record(minted[0])['meta'].get('aliases') or []
        self.assertIn('[[Old Name]]', aliases)   # the embedded wikilink intact
        self.assertIn('grandpa-bob', aliases)

    def test_mint_write_guard_refuses_rather_than_corrupt(self) -> None:
        # The re-parse guard is a backstop: if a rewrite ever produced a
        # frontmatter that no longer parses as a mapping, --fix-ids must REFUSE
        # (leave the file, name it in progress), never write the corrupt text.
        import unittest.mock as mock
        (self.root / 'people' / 'Grandpa Bob.md').write_text(
            '---\nname: Grandpa Bob\nliving: false\n---\n\n# Grandpa Bob\n',
            encoding='utf-8')
        before = (self.root / 'people' / 'Grandpa Bob.md').read_text(encoding='utf-8')
        with mock.patch.object(
                lint, '_insert_id_and_aliases',
                lambda *a, **k: ('---\n: : broken\n- x\n bad:\n---\n\nbody\n', True)):
            result = lint.run_lint(self.root, {}, fix_ids=True)
        # Nothing minted, original untouched, and the refusal names the file.
        self.assertEqual(list((self.root / 'people').glob('bob__grandpa_P-*.md')), [])
        self.assertEqual((self.root / 'people' / 'Grandpa Bob.md').read_text(encoding='utf-8'), before)
        self.assertTrue(any('refused to mint' in l for l in result.data['progress']),
                        result.data['progress'])


class DirectLineStubW119Tests(unittest.TestCase):
    """W119: direct-line ancestors still filed as stubs (the lint mirror of
    `fha views brackets` check 4 - the established lint-detects /
    brackets-fixes split W103/W110 follow).

    Warning severity always - on a live archive this fires for every
    not-yet-curated ancestor at once, and it must read as a research lead
    (SPEC §4: a stub is a legitimate permanent state), never a defect. The
    message names `fha views brackets --fix-promote` as the applying fix.
    """

    KID = 'P-3aaaaaaaaa'
    PA = 'P-3bbbbbbbbb'
    FRIEND = 'P-3ccccccccc'
    SID = 'S-3aaaaaaaaa'

    def _ptext(self, pid: str, name: str, sex: str = 'U', tier: str = 'stub') -> str:
        return (f'---\nid: {pid}\nname: {name}\nsex: {sex}\nliving: false\n'
                f'tier: {tier}\n---\n\n# {name}\n\n## Biography\n\nx\n')

    def _build(self, *, root_person: bool = True, pa_tier: str = 'stub',
               pa_in_stubs: bool = True) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / 'people' / 'stubs').mkdir(parents=True)
        (root / 'people' / '002 Pa Mirror').mkdir(parents=True)
        (root / 'sources' / 'notes').mkdir(parents=True)
        cfg = f'root_person: {self.KID}\n' if root_person else ''
        (root / 'fha.yaml').write_text(
            cfg + 'roots:\n  documents: documents\n', encoding='utf-8')
        (root / 'people' / '002 Pa Mirror' / f'mirror__kid_{self.KID}.md').write_text(
            self._ptext(self.KID, 'Kid Mirror', 'F', 'curated'), encoding='utf-8')
        pa_dir = (root / 'people' / 'stubs') if pa_in_stubs else (root / 'people' / '002 Pa Mirror')
        (pa_dir / f'mirror__pa_{self.PA}.md').write_text(
            self._ptext(self.PA, 'Pa Mirror', 'M', pa_tier), encoding='utf-8')
        (root / 'people' / 'stubs' / f'far__frank_{self.FRIEND}.md').write_text(
            self._ptext(self.FRIEND, 'Frank Far', 'M'), encoding='utf-8')
        claim = (
            f'- value: "{self.KID} child of {self.PA}"\n'
            f'  id: C-3aaaaaaaaa\n  type: relationship\n  subtype: biological\n'
            f'  persons: [{self.KID}, {self.PA}]\n  roles:\n'
            f'    child: {self.KID}\n    parent: [{self.PA}]\n'
            f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
            f'  information: primary\n  evidence: direct\n  notes: x.\n'
        )
        (root / 'sources' / 'notes' / f'rel_{self.SID.lower()}.md').write_text(
            f'---\nid: {self.SID}\ntitle: Rel\nsource_type: other\n---\n\n'
            f'## Claims\n```yaml\n{claim}```\n', encoding='utf-8')
        return root

    def _w119(self, root: Path) -> list:
        from _lib import load_fha_yaml
        findings, _reg = lint._run_lint_core(root, load_fha_yaml(root))
        return [f for f in findings if f.code == 'W119']

    def test_direct_line_stub_warns_naming_fix_promote(self) -> None:
        w119 = self._w119(self._build())
        self.assertEqual(len(w119), 1)   # PA only - never off-line FRIEND
        f = w119[0]
        self.assertEqual(f.severity, 'W')
        self.assertIn('research lead, not a defect', f.message)
        self.assertIn('fha views brackets --fix-promote', f.message)
        self.assertIn(f'fha person promote {self.PA}', f.message)

    def test_curated_record_parked_in_stubs_still_flagged(self) -> None:
        # A half promotion (tier flipped by hand, never moved) is still W119.
        w119 = self._w119(self._build(pa_tier='curated', pa_in_stubs=True))
        self.assertEqual(len(w119), 1)

    def test_fully_curated_ancestor_is_clean(self) -> None:
        w119 = self._w119(self._build(pa_tier='curated', pa_in_stubs=False))
        self.assertEqual(w119, [])

    def test_no_root_person_stays_silent(self) -> None:
        w119 = self._w119(self._build(root_person=False))
        self.assertEqual(w119, [])

    def test_blank_name_falls_back_to_the_pid_not_the_string_none(self) -> None:
        # Codex-shape audit past #157: `.get('name', pid)`'s pid fallback
        # only fires when the key is missing - a hand-blanked `name:` line
        # (YAML null, key present) str()-converts to the literal text
        # 'None' first, so the message would otherwise read "None
        # (Ahnentafel N) is a direct-line ancestor..." instead of falling
        # back to the id the way an actually nameless record already does.
        root = self._build()
        pa_path = root / 'people' / 'stubs' / f'mirror__pa_{self.PA}.md'
        pa_path.write_text(
            f'---\nid: {self.PA}\nname:\nsex: M\nliving: false\ntier: stub\n'
            '---\n\n# Pa Mirror\n\n## Biography\n\nx\n', encoding='utf-8')
        w119 = self._w119(root)
        self.assertEqual(len(w119), 1)
        self.assertIn(self.PA.lower(), w119[0].message)
        self.assertNotIn('None (Ahnentafel', w119[0].message)


class AhnentafelPlacementBlankNameTests(unittest.TestCase):
    """W110 (wrong couple folder) and W120 (sex-defaulted slot) share
    `_check_ahnentafel_placement`, and both built their finding message off
    the same broken `str(registry.person_meta.get(pid, {}).get('name',
    pid))` shape `DirectLineStubW119Tests` guards for W119: the `pid`
    fallback only fires when `name:` is missing, not when it is present but
    blank, so a hand-blanked name would show as the literal text 'None'
    instead of the id."""

    KID = 'P-4aaaaaaaaa'   # root_person
    PA = 'P-4bbbbbbbbb'    # KID's sole linked parent - blank name, no sex
    SID = 'S-4aaaaaaaaa'

    def _build(self) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / 'people' / '001 Kid Mirror').mkdir(parents=True)
        # No `sex:` at all (triggers W120's "lone parent, sex defaulted"),
        # and `name:` written blank rather than omitted or filled in - the
        # explicit-null shape the pid fallback does not catch.
        (root / 'people' / '099 Wrong Folder').mkdir(parents=True)
        (root / 'sources' / 'notes').mkdir(parents=True)
        (root / 'fha.yaml').write_text(
            f'root_person: {self.KID}\nroots:\n  documents: documents\n',
            encoding='utf-8')
        (root / 'people' / '001 Kid Mirror' / f'mirror__kid_{self.KID}.md').write_text(
            f'---\nid: {self.KID}\nname: Kid Mirror\nsex: F\nliving: false\n'
            'tier: curated\n---\n\n# Kid Mirror\n\n## Biography\n\nx\n',
            encoding='utf-8')
        (root / 'people' / '099 Wrong Folder' / f'mirror__pa_{self.PA}.md').write_text(
            f'---\nid: {self.PA}\nname:\nliving: false\ntier: stub\n'
            '---\n\n# Blank Name Parent\n', encoding='utf-8')
        claim = (
            f'- value: "{self.KID} child of {self.PA}"\n'
            f'  id: C-4aaaaaaaaa\n  type: relationship\n  subtype: biological\n'
            f'  persons: [{self.KID}, {self.PA}]\n  roles:\n'
            f'    child: {self.KID}\n    parent: [{self.PA}]\n'
            f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
            f'  information: primary\n  evidence: direct\n  notes: x.\n'
        )
        (root / 'sources' / 'notes' / f'rel_{self.SID.lower()}.md').write_text(
            f'---\nid: {self.SID}\ntitle: Rel\nsource_type: other\n---\n\n'
            f'## Claims\n```yaml\n{claim}```\n', encoding='utf-8')
        return root

    def _findings(self, root: Path, code: str) -> list:
        from _lib import load_fha_yaml
        findings, _reg = lint._run_lint_core(root, load_fha_yaml(root))
        return [f for f in findings if f.code == code]

    def test_w110_names_the_pid_not_the_string_none(self) -> None:
        root = self._build()
        w110 = self._findings(root, 'W110')
        self.assertEqual(len(w110), 1)
        self.assertIn(self.PA.lower(), w110[0].message)
        self.assertNotIn('None (Ahnentafel', w110[0].message)

    def test_w120_names_the_pid_not_the_string_none(self) -> None:
        root = self._build()
        w120 = self._findings(root, 'W120')
        self.assertEqual(len(w120), 1)
        self.assertIn(self.PA.lower(), w120[0].message)
        self.assertNotIn('None (Ahnentafel', w120[0].message)


class KeywordScanRespectsPhotosIgnoreTests(unittest.TestCase):
    """E012's exiftool pass honours `photos_ignore:` like the catalog scan.

    Without this the check reads every file in the bulk photo-service export
    the setting exists to exclude - the motivating archive (#35) has 63,156 of
    them - and hands them all to exiftool for keywords nobody filed.
    """

    def _roots(self, photos_ignore=None):
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, tmp, True)
        root = Path(tmp)
        photos = root / 'photos'
        (photos / 'Woodbury').mkdir(parents=True)
        (photos / 'Flickr Export' / 'deep').mkdir(parents=True)
        (photos / 'Woodbury' / 'keep.jpg').write_bytes(b'x')
        (photos / 'Flickr Export' / 'bulk.jpg').write_bytes(b'x')
        (photos / 'Flickr Export' / 'deep' / 'deeper.jpg').write_bytes(b'x')
        cfg = {} if photos_ignore is None else {'photos_ignore': photos_ignore}
        return root, photos, lint.Registry(root, cfg)

    def _scanned(self, photos_ignore=None):
        root, photos, registry = self._roots(photos_ignore)
        return {
            p.relative_to(photos).as_posix()
            for p in lint._files_to_keyword_scan('photos', photos, registry)
        }

    def test_without_the_setting_every_photo_is_scanned(self) -> None:
        self.assertEqual(self._scanned(), {
            'Woodbury/keep.jpg',
            'Flickr Export/bulk.jpg',
            'Flickr Export/deep/deeper.jpg',
        })

    def test_an_ignored_subtree_is_skipped_wholesale(self) -> None:
        self.assertEqual(self._scanned('Flickr Export/*'), {'Woodbury/keep.jpg'})

    def test_matching_is_case_insensitive_like_the_scan(self) -> None:
        # The photo library sits on a case-insensitive filesystem on Windows
        # and macOS; 'flickr export' failing to prune reads as a broken feature.
        self.assertEqual(self._scanned('flickr export/*'), {'Woodbury/keep.jpg'})

    def test_a_malformed_setting_prunes_nothing_rather_than_guessing(self) -> None:
        # photoindex is where the human is shown the real parse error.
        self.assertEqual(len(self._scanned(12345)), 3)

    def test_documents_ignores_the_photos_setting(self) -> None:
        root, _photos, registry = self._roots('Flickr Export/*')
        docs = root / 'documents' / 'Flickr Export'
        docs.mkdir(parents=True)
        (docs / 'deed.pdf').write_bytes(b'x')
        found = {
            p.name
            for p in lint._files_to_keyword_scan(
                'documents', root / 'documents', registry)
        }
        self.assertEqual(found, {'deed.pdf'})


_MARIE_PROFILE = (
    '---\nid: P-3kq9v8x2m1\nname: Marie Timeline Hartley\nliving: false\n'
    'sex: F\ntier: curated\n---\n\n# Marie Timeline Hartley\n\n'
    '## Biography\n\nShe kept the farm books.\n'
)

_MARIE_TIMELINE = (
    '<!-- GENERATED by fha views timeline on 2026-06-30'
    ' - do not edit; regenerate instead -->\n\n'
    '# Timeline: Marie Hartley\n\n- 1880 - birth: born in Fairview\n'
)


class ContentDecidesPersonKindTests(unittest.TestCase):
    """A person named Marie Timeline was invisible, and lint said so clean.

    SPEC §13's kind slot and the last given-name segment are one slot, so
    `hartley__marie_timeline_P-….md` reads as a generated timeline. Lint filed
    her under the companion paths, skipped every §9 profile check, and printed
    `✓ No issues found.` while she had no `persons` row anywhere in the
    archive. Content now decides here exactly as it does in the index, and the
    remaining ambiguity is reported (W122) instead of being resolved silently.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, filename: str, text: str) -> Path:
        path = self.root / 'people' / filename
        path.write_text(text, encoding='utf-8')
        return path

    def _codes(self, findings) -> list[str]:
        return [f.code for f in findings]

    def test_a_person_named_timeline_registers_as_a_profile(self) -> None:
        path = self._write('hartley__marie_timeline_P-3kq9v8x2m1.md', _MARIE_PROFILE)
        _findings, reg = lint._run_lint_core(self.root, {})
        self.assertEqual(reg.person_profile_paths.get('p-3kq9v8x2m1'), [path])
        self.assertEqual(reg.person_companion_paths.get('p-3kq9v8x2m1', []), [])

    def test_w122_fires_on_the_contradiction(self) -> None:
        self._write('hartley__marie_timeline_P-3kq9v8x2m1.md', _MARIE_PROFILE)
        findings, _reg = lint._run_lint_core(self.root, {})
        w122 = [f for f in findings if f.code == 'W122']
        self.assertEqual(len(w122), 1)
        text = w122[0].message
        # Plain language for a non-technical genealogist: the reading is
        # correct, the rename is named, and keeping the name is allowed.
        self.assertIn('hartley__marie_P-3kq9v8x2m1.md', text)
        self.assertIn('timeline', text)
        for jargon in ('frontmatter', 'companion', 'kind slot', 'parse'):
            self.assertNotIn(jargon, text.lower())

    def test_w122_asks_for_a_name_when_there_is_none_to_suggest(self) -> None:
        # Someone whose one given name IS the word: dropping it would leave
        # `hartley___P-…`, so the message must ask for a name instead of
        # proposing a broken one.
        self._write(
            'hartley__timeline_P-3kq9v8x2m1.md',
            '---\nid: P-3kq9v8x2m1\nname: Timeline Hartley\nliving: false\n---\n\n# T\n')
        findings, _reg = lint._run_lint_core(self.root, {})
        text = [f for f in findings if f.code == 'W122'][0].message
        self.assertNotIn('hartley___P-', text)
        self.assertIn('give the file a name', text)

    def test_w122_is_silent_for_a_real_generated_companion(self) -> None:
        self._write('hartley__marie_P-3kq9v8x2m1.md', _MARIE_PROFILE)
        self._write('hartley__marie_timeline_P-3kq9v8x2m1.md', _MARIE_TIMELINE)
        findings, _reg = lint._run_lint_core(self.root, {})
        self.assertNotIn('W122', self._codes(findings))

    def test_w122_is_silent_for_an_ordinary_profile(self) -> None:
        self._write('hartley__marie_P-3kq9v8x2m1.md', _MARIE_PROFILE)
        findings, _reg = lint._run_lint_core(self.root, {})
        self.assertNotIn('W122', self._codes(findings))

    def test_lint_no_longer_reports_the_archive_clean(self) -> None:
        # The whole defect in one line: the only person in the archive had no
        # index row, and `fha lint` said there was nothing to see.
        self._write('hartley__marie_timeline_P-3kq9v8x2m1.md', _MARIE_PROFILE)
        result = lint.run_lint(self.root, {})
        buf = io.StringIO()
        with redirect_stdout(buf):
            lint._cmd_lint(result, self.root)
        out = buf.getvalue()
        self.assertNotIn('✓ No issues found.', out)
        self.assertIn('W122', out)

    def test_a_research_name_over_a_person_record_is_not_research(self) -> None:
        # SPEC §16 homes ## Open Questions in the research companion. A person
        # record that happens to be named like one is not a research file, so
        # its body must stay out of the E009 research scope. ## Hypotheses is
        # different (#56): the archive already writes it into profiles too,
        # so her own section here is a real record whether or not the file
        # also happens to be named like a research companion.
        self._write(
            'smith__anne_research_P-3333333333.md',
            '---\nid: P-3333333333\nname: Anne Research Smith\nliving: false\n---\n\n'
            '## Open Questions\n\n## Q: Which ship did she arrive on?\n'
            '- origin: human\n- status: open\n\n'
            '## Hypotheses\n\n- id: H-abcabcabca\n'
            '  hypothesis: "arrived by ~1869"\n  origin: agent\n  status: open\n')
        findings, reg = lint._run_lint_core(self.root, {})
        self.assertEqual(list(reg.research_content), [])
        self.assertIn('h-abcabcabca', reg.hypothesis_ids)
        self.assertIn('W122', self._codes(findings))


def _scandir_denying(unreadable: Path):
    """An os.scandir stand-in that refuses to list `unreadable`.

    The fault goes in at `os.scandir` because `os.walk` resolves it at call
    time on every supported Python - that is what makes the `onerror` seam
    observable here. chmod cannot produce this: CI runs as root, which ignores
    mode bits, and Windows has no equivalent.

    What this deliberately does NOT rely on: that pathlib's `rglob` reaches the
    disk the same way. It does on 3.11/3.12/3.14, but NOT on the 3.10 floor
    (pathlib routes through an accessor object that bound `os.scandir` at
    import time, so a later patch is invisible) and not on 3.13. So the
    injection does not reproduce the pre-fix `rglob` behaviour on every version
    we support - a regression back to `rglob` is still caught everywhere, but
    on the floor it is caught by the warning going missing rather than by the
    folder reading as empty.
    """
    real_scandir = os.scandir
    target = unreadable.resolve()

    def scandir(path='.'):
        try:
            denied = Path(path).resolve() == target
        except (TypeError, ValueError, OSError):
            denied = False
        if denied:
            err = PermissionError(13, 'Permission denied')
            err.filename = str(path)
            raise err
        return real_scandir(path)

    return scandir


class UnreadableRecordFolderTests(unittest.TestCase):
    """W123: lint must not certify an archive it could not fully read.

    `fha lint` sells one sentence - "your archive matches the spec" - and Pass
    1's `rglob` handed it that sentence over any subtree that would not list.
    Every record filed there went unchecked and the summary still read "0
    errors", which is the most confident possible way to say nothing at all."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.folder = self.root / 'people' / '040 Hartley'
        self.folder.mkdir(parents=True)
        (self.root / 'sources').mkdir()
        (self.root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        (self.folder / 'hartley__cur_P-aaaaaaaaaa.md').write_text(
            '---\nid: P-aaaaaaaaaa\nname: Cur Hartley\nliving: false\n'
            'tier: curated\nno_known_marriages: true\n---\n\n'
            '## Biography\n\nx\n', encoding='utf-8')

    def test_a_folder_that_will_not_open_is_reported_not_certified(self) -> None:
        clean, reg = lint._run_lint_core(self.root, {})
        self.assertNotIn('W123', [f.code for f in clean])
        self.assertEqual(reg.unreadable_dirs, [])
        self.assertIn('p-aaaaaaaaaa', reg.person_profile_paths)

        with unittest.mock.patch('os.scandir', new=_scandir_denying(self.folder)):
            findings, reg = lint._run_lint_core(self.root, {})

        # Pre-fix: the folder's record simply was not there, no W123 was
        # raised, and lint reported an archive it had never opened as clean.
        self.assertNotIn('p-aaaaaaaaaa', reg.person_profile_paths)
        w123 = [f for f in findings if f.code == 'W123']
        self.assertEqual(len(w123), 1)
        self.assertEqual(w123[0].severity, 'W')
        self.assertIn('people/040 Hartley', w123[0].message)
        self.assertIn('fha lint', w123[0].message)

    def test_run_lint_leaves_exit_0_behind(self) -> None:
        # The point of the warning: a clean bill of health must not be issued
        # over records nobody read, so the run moves off exit 0.
        #
        # W123 is asserted BY CODE, not by a warning count, and the difference
        # is the whole test. `n_warnings >= 1` passed against the unfixed lint
        # on Python 3.10 - the floor, and a CI leg - for a reason that has
        # nothing to do with this guarantee: pre-fix lint reached records
        # through `rglob`, which on 3.10 does not observe a patched
        # `os.scandir` (pathlib binds it at import time there). So the denied
        # record stayed visible, its missing vitals raised W101, and one
        # warning was enough to satisfy the assertion. On 3.14 the same test
        # failed honestly. A count is not evidence when the fixture can supply
        # the count by itself; the code is.
        with unittest.mock.patch('os.scandir', new=_scandir_denying(self.folder)):
            result = lint.run_lint(self.root, {})
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertIn('W123', [m.code for m in result.messages])


_W124_SID = 'S-5n7q9s1t3v'
_W124_SOURCE = '''---
id: {sid}
title: Hand-drawn family chart
source_type: other
files:{files}
---

## Claims
```yaml
- id: C-5n7q9s1t3v
  type: name
  persons: ["Rose Harkness"]
  value: "Rose Harkness"
  status: {status}
  confidence: high
  reviewed: 2026-08-01
```

## Notes
Twenty-two pages, all picture.
'''

_IMAGE = (f'documents/charts/harkness-chart_{_W124_SID}.jpg', 'front')
_TRANSCRIPT = (f'documents/charts/harkness-chart-transcript_{_W124_SID}.md',
               'transcript')


class UntranscribedEvidenceTests(unittest.TestCase):
    """W124: accepted claims resting on evidence the archive holds no words for.

    A source can be processed, mined from its pictures, reviewed and accepted,
    and the archive still holds no text of what the document says. Nothing in
    the tools noticed, and nothing warned - so a text search over such an
    archive answered for what an earlier pass had chosen to write down while
    looking exactly like a search of the evidence. The archive that produced
    #46 carried 43 such sources and 135 accepted claims on them."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        (self.root / 'people').mkdir()
        (self.root / 'sources' / 'other').mkdir(parents=True)

    def _write_source(self, entries: list, status: str = 'accepted') -> None:
        """Write the source record AND the files it lists, so the only finding
        under test is W124 - a `files:` line with nothing behind it is E011."""
        block = ''.join(
            f'\n  - file: {path}\n    role: {role}' for path, role in entries)
        (self.root / 'sources' / 'other'
         / f'harkness-chart_{_W124_SID.lower()}.md').write_text(
            _W124_SOURCE.format(sid=_W124_SID, files=block, status=status),
            encoding='utf-8')
        for path, _role in entries:
            on_disk = self.root / path
            on_disk.parent.mkdir(parents=True, exist_ok=True)
            on_disk.write_text('Rose Harkness, married 1871.\n', encoding='utf-8')

    def _codes(self) -> list:
        findings, _ = lint._run_lint_core(self.root, {})
        return [f for f in findings if f.code == 'W124']

    def test_an_image_only_source_with_an_accepted_claim_is_flagged(self) -> None:
        self._write_source([_IMAGE])
        found = self._codes()
        self.assertEqual(len(found), 1, [f.message for f in found])
        self.assertEqual(found[0].severity, 'W')
        # The next-step rule: the message names what to do, in commands that
        # exist. `fha source transcribe` does not.
        self.assertIn('1 accepted claim(s)', found[0].message)
        self.assertIn(_W124_SID, found[0].message)
        self.assertIn('fha source extract', found[0].message)
        self.assertIn('--more', found[0].message)
        self.assertNotIn('fha source transcribe', found[0].message)

    def test_a_transcript_beside_the_scan_settles_it(self) -> None:
        self._write_source([_IMAGE, _TRANSCRIPT])
        self.assertEqual(self._codes(), [])

    def test_suggested_claims_alone_are_not_flagged(self) -> None:
        # Nothing has been accepted on the strength of an unread picture yet -
        # review is where that gets settled, and W102 already names the backlog.
        self._write_source([_IMAGE], status='suggested')
        self.assertEqual(self._codes(), [])

    def test_a_source_with_no_files_is_not_flagged(self) -> None:
        # An accepted claim from an online record with nothing attached has no
        # evidence file to transcribe; there is nothing here to fix.
        self._write_source([])
        self.assertEqual(self._codes(), [])

    def test_the_warning_moves_the_run_off_exit_0(self) -> None:
        self._write_source([_IMAGE])
        result = lint.run_lint(self.root, {})
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertIn('W124', [m.code for m in result.messages])


_E011_SID = 'S-5n7q9s1t3v'
_E011_SOURCE = '''---
id: {sid}
title: Escaping Source
source_type: other
files:{files}
---

## Claims
```yaml
```
'''


class InventoryContainmentE011Tests(unittest.TestCase):
    """E011: a `files:` entry that resolves OUTSIDE the configured
    documents/photos roots (round-2 #163 audit finding).

    Before this, E011 tested only `resolved.exists()` and had nothing to say
    about a hand-edited entry (a `..` segment, or a doubled slash) that
    resolves to a real file elsewhere on disk - the exact diagnostic `fha
    process refile`'s and `fha packet`'s own containment checks point at
    (`Run \\`fha lint\\``), which reported nothing for exactly this shape.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n  photos: photos\n', encoding='utf-8')
        (self.root / 'people').mkdir()
        (self.root / 'sources' / 'other').mkdir(parents=True)
        (self.root / 'documents' / 'other').mkdir(parents=True)

    def _write_source(self, file_line: str) -> None:
        block = f'\n{file_line}' if file_line else ''
        (self.root / 'sources' / 'other' / f'escaping-source_{_E011_SID}.md').write_text(
            _E011_SOURCE.format(sid=_E011_SID, files=block), encoding='utf-8')

    def _e011_findings(self) -> list:
        findings, _ = lint._run_lint_core(self.root, {})
        return [f for f in findings if f.code == 'E011']

    def test_traversal_entry_is_flagged_even_though_it_resolves_somewhere(self) -> None:
        # An escaping path that ALSO exists on disk (just outside the
        # archive) must still be flagged - an exists()-only test would pass
        # it clean, which is exactly the gap this closes.
        outside = self.root.parent / 'outside-secret.tif'
        outside.write_bytes(b'not part of the archive')
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        self._write_source('  - file: documents/../../outside-secret.tif\n    role: primary')

        found = self._e011_findings()

        self.assertEqual(len(found), 1, [f.message for f in found])
        self.assertEqual(found[0].severity, 'E')
        self.assertIn('resolves outside', found[0].message)
        self.assertIn("outside-secret.tif", found[0].message)
        # Names the actual fix (a hand edit), not `fha reconcile` - reconcile
        # only re-links a files: entry whose path no longer resolves, and
        # this one does resolve (just to the wrong place).
        self.assertIn('by hand', found[0].message)

    def test_traversal_entry_to_a_missing_target_is_still_flagged(self) -> None:
        # The escaping path need not even resolve to a real file - containment
        # is checked before existence, so a '..'-entry pointing at nothing is
        # reported as escaping, not as an ordinary "not found on disk" E011.
        self._write_source(
            '  - file: documents/../../nowhere-at-all.tif\n    role: primary')

        found = self._e011_findings()

        self.assertEqual(len(found), 1, [f.message for f in found])
        self.assertIn('resolves outside', found[0].message)

    def test_ordinary_entry_inside_documents_root_is_not_flagged(self) -> None:
        asset = self.root / 'documents' / 'other' / f'escaping-source_{_E011_SID}.jpg'
        asset.write_bytes(b'jpegbytes')
        self._write_source(
            f'  - file: documents/other/escaping-source_{_E011_SID}.jpg\n    role: primary')

        self.assertEqual(self._e011_findings(), [])


class UnscopedCoupleClaimW125Tests(unittest.TestCase):
    """W125: a marriage/divorce claim naming more than two people with no
    `roles: spouse:` map.

    The indexer refuses to guess which two of six people married each other and
    records no spouse edge at all (index.py `_spouse_parties`). That is the
    right call - a false marriage is read back as fact by `fha relate` and the
    family charts, while a missing one is merely missing - but it is silent,
    and a couple quietly absent from the tree is its own kind of wrong. This
    warning is what makes the silence visible; without it the fix would trade a
    loud error for a quiet one.
    """

    HUS = 'P-h1h1h1h1h1'
    WIF = 'P-w2w2w2w2w2'
    PARENTS = ['P-f3f3f3f3f3', 'P-m4m4m4m4m4', 'P-f5f5f5f5f5', 'P-m6m6m6m6m6']
    SID = 'S-7777777777'

    def _build(self, *, ctype: str = 'marriage', persons=None,
               roles_block: str = '', status: str = 'accepted',
               negated: bool = False) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / 'people').mkdir(parents=True)
        (root / 'sources' / 'notes').mkdir(parents=True)
        (root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                       encoding='utf-8')
        everyone = [self.HUS, self.WIF] + self.PARENTS
        for n, pid in enumerate(everyone):
            (root / 'people' / f'x__p{n}_{pid}.md').write_text(
                f'---\nid: {pid}\nname: Person {n}\nsex: U\nliving: false\n'
                f'tier: stub\n---\n\n# Person {n}\n', encoding='utf-8')
        named = everyone if persons is None else persons
        claim = (f'- value: "a {ctype}"\n'
                 f'  id: C-1111111111\n'
                 f'  type: {ctype}\n'
                 f'  persons: [{", ".join(named)}]\n'
                 f'  status: {status}\n  reviewed: 2026-01-01\n'
                 f'  confidence: high\n  date: 1890\n'
                 + ('  negated: true\n  evidence: negative\n' if negated
                    else '  information: primary\n  evidence: direct\n')
                 + '  notes: x.\n'
                 + roles_block)
        (root / 'sources' / 'notes' / f'rec_{self.SID.lower()}.md').write_text(
            f'---\nid: {self.SID}\ntitle: Rec\nsource_type: vital-record\n---\n\n'
            f'## Claims\n```yaml\n{claim}```\n', encoding='utf-8')
        return root

    def _w125(self, root: Path) -> list:
        from _lib import load_fha_yaml
        findings, _reg = lint._run_lint_core(root, load_fha_yaml(root))
        return [f for f in findings if f.code == 'W125']

    def test_six_person_marriage_without_roles_warns(self) -> None:
        w = self._w125(self._build())
        self.assertEqual(len(w), 1)
        f = w[0]
        self.assertEqual(f.severity, 'W')
        # Says what is wrong, what it costs, and the concrete repair.
        self.assertIn('names 6 people', f.message)
        self.assertIn('which two of them were the couple', f.message)
        self.assertIn('fha relate', f.message)
        self.assertIn('spouse: [P-', f.message)
        # The repair must never be "delete the parents from persons:".
        self.assertIn('Leave everyone in persons:', f.message)

    def test_six_person_divorce_without_roles_warns(self) -> None:
        w = self._w125(self._build(ctype='divorce'))
        self.assertEqual(len(w), 1)
        self.assertIn('no marriage is recorded as ending', w[0].message)

    def test_roles_map_naming_the_couple_is_clean(self) -> None:
        roles = f'  roles:\n    spouse: [{self.HUS}, {self.WIF}]\n'
        self.assertEqual(self._w125(self._build(roles_block=roles)), [])

    def test_two_person_claim_without_roles_is_clean(self) -> None:
        # The ordinary hand-written marriage claim - the indexer's two-person
        # fallback handles it, so there is nothing to warn about.
        self.assertEqual(
            self._w125(self._build(persons=[self.HUS, self.WIF])), [])

    def test_legacy_spouse_of_relationship_warns(self) -> None:
        # `relationship` + `subtype: spouse-of` derives spouse edges through the
        # same rule, so it earns the same warning. A `roles:` map that names no
        # resolvable spouse (here the list shorthand) leaves the claim in the
        # identical could-not-tell state.
        root = self._build(ctype='relationship',
                           roles_block='  subtype: spouse-of\n  roles: [spouse, spouse]\n')
        w = self._w125(root)
        self.assertEqual(len(w), 1)
        self.assertIn('no marriage is recorded between any of them', w[0].message)

    def test_ordinary_parent_child_claim_never_warns(self) -> None:
        # The false positive this rule must not have: a normal relationship
        # claim names a child and two parents - three people, no spouse role,
        # and nothing whatever wrong with it.
        roles = (f'  subtype: biological\n  roles:\n    child: {self.HUS}\n'
                 f'    parent: [{self.PARENTS[0]}, {self.PARENTS[1]}]\n')
        root = self._build(ctype='relationship',
                           persons=[self.HUS, self.PARENTS[0], self.PARENTS[1]],
                           roles_block=roles)
        self.assertEqual(self._w125(root), [])

    def test_roles_map_naming_one_spouse_still_warns(self) -> None:
        # A roles: map that resolves to a single spouse - one typo'd id, one
        # spouse left out of persons: - has not said who the couple were, and
        # the indexer derives nothing from it (_lib.spouse_parties). W125 tests
        # the derivation rule itself, not the mere presence of a roles: key, so
        # this silence is reported like any other.
        roles = f'  roles:\n    spouse: [{self.HUS}]\n'
        self.assertEqual(len(self._w125(self._build(roles_block=roles))), 1)

    def test_roles_map_naming_three_spouses_is_clean(self) -> None:
        # Successive marriages recorded on one claim: the map HAS answered the
        # question and every pairing is derived, so there is nothing to warn.
        roles = ('  roles:\n    spouse: '
                 f'[{self.HUS}, {self.WIF}, {self.PARENTS[0]}]\n')
        self.assertEqual(self._w125(self._build(roles_block=roles)), [])

    def test_suggested_claim_does_not_warn(self) -> None:
        # Relationship derivation reads `accepted` claims only, so a suggested
        # claim derives nothing whatever its roles: map says. Warning that a
        # couple is missing from the tree because of a claim nobody has accepted
        # yet points at the wrong repair: the repair is review (W102 already
        # tracks that backlog), not a roles: map. The warning becomes true the
        # day the claim is accepted, and fires then.
        self.assertEqual(self._w125(self._build(status='suggested')), [])

    def test_needs_review_claim_does_not_warn(self) -> None:
        self.assertEqual(self._w125(self._build(status='needs-review')), [])

    def test_negated_claim_does_not_warn(self) -> None:
        # A negated marriage is a researched absence - "we looked, and these
        # people did not marry" (SPEC §8.6). Derivation skips it deliberately.
        # Warning here would tell the human a marriage is missing from the tree
        # about a claim whose whole content is that the marriage never happened.
        self.assertEqual(
            self._w125(self._build(negated=True)), [])

    def test_accepted_claim_still_warns(self) -> None:
        # The control for the three above: the status that DOES derive edges
        # keeps its warning.
        self.assertEqual(len(self._w125(self._build())), 1)

    def test_two_person_claim_with_a_contradictory_role_warns(self) -> None:
        # The shape the `len(named) > 2` heuristic could never see. This claim
        # names two people and calls one of them a parent, so the indexer
        # derives no couple from it (it must not contradict the claim's own
        # words) - and the marriage is then missing from the tree with nothing
        # said about it. W125's real subject is the derivation rule: a couple
        # claim that resolves two or more people and yields no couple.
        roles = f'  roles:\n    spouse: [{self.HUS}]\n    parent: [{self.WIF}]\n'
        w = self._w125(self._build(persons=[self.HUS, self.WIF], roles_block=roles))
        self.assertEqual(len(w), 1)
        msg = w[0].message
        self.assertIn('names 2 people', msg)
        # The wording has to fit THIS shape, not the six-person certificate:
        # "does not say which two of them were the couple" would be false here.
        self.assertNotIn('which two of them', msg)
        self.assertIn('a parent', msg)
        self.assertIn('no marriage is recorded between them', msg)
        self.assertIn('spouse: [P-', msg)

    def test_two_person_divorce_with_a_contradictory_role_warns(self) -> None:
        roles = f'  roles:\n    spouse: [{self.HUS}]\n    parent: [{self.WIF}]\n'
        w = self._w125(self._build(ctype='divorce', persons=[self.HUS, self.WIF],
                                   roles_block=roles))
        self.assertEqual(len(w), 1)
        # A divorce costs a marriage not recorded as ENDING, whatever the shape.
        self.assertIn('no marriage is recorded as ending', w[0].message)

    def test_duplicate_persons_entry_does_not_warn(self) -> None:
        # A bare P-id and a name-link for the same person are two persons:
        # entries and ONE person. There is no couple here and nothing a roles:
        # map could add, so W125 has nothing to say - counting entries instead
        # of people would demand a spouse pair from a claim that names one man.
        root = self._build(persons=[self.HUS, '"[[Person 0]]"'])
        self.assertEqual(self._w125(root), [])

    def test_duplicate_persons_entry_beside_a_spouse_does_not_warn(self) -> None:
        # Three entries, two people - the ordinary two-person claim wearing a
        # duplicate. The indexer derives the pair, so there is no silence to
        # report.
        root = self._build(persons=[self.HUS, '"[[Person 0]]"', self.WIF])
        self.assertEqual(self._w125(root), [])

    def test_list_form_roles_does_not_crash_lint(self) -> None:
        # `roles: [spouse, spouse]` is the shorthand lint's OWN E015 message
        # suggests. It names no person, so it cannot scope the couple (W125
        # still fires), but it must never raise: a traceback on the human's
        # screen is always a defect (AGENTS.md - "Who you serve").
        w = self._w125(self._build(roles_block='  roles: [spouse, spouse]\n'))
        self.assertEqual(len(w), 1)


class BirthClaimWithoutParentageW126Tests(unittest.TestCase):
    """W126: an accepted birth claim that names other people but says nothing
    about which of them are the parents.

    A birth record is where an archive states parentage most plainly - "born to
    X and Y" - but only the `roles:` map says which entry is the child and which
    are the parents. Without one the indexer derives NO parent edge rather than
    reading the persons: order as a contract (index.py `_derive_relationships`,
    `_lib.parentage_parties`). That refusal is right - a false parent is read
    back as fact by `fha relate`, the tree views, `fha report` and the GEDCOM
    export - but on its own it just moves a silently-wrong archive to a
    silently-inert one. This warning is the other half of the fix (issue #71),
    exactly as W125 is for couple claims.
    """

    CHILD = 'P-c1c1c1c1c1'
    FATHER = 'P-f2f2f2f2f2'
    MOTHER = 'P-m3m3m3m3m3'
    SID = 'S-7777777777'

    def _build(self, *, ctype: str = 'birth', persons=None,
               roles_block: str = '', status: str = 'accepted',
               negated: bool = False) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / 'people').mkdir(parents=True)
        (root / 'sources' / 'notes').mkdir(parents=True)
        (root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                       encoding='utf-8')
        everyone = [self.CHILD, self.FATHER, self.MOTHER]
        for n, pid in enumerate(everyone):
            (root / 'people' / f'x__p{n}_{pid}.md').write_text(
                f'---\nid: {pid}\nname: Person {n}\nsex: U\nliving: false\n'
                f'tier: stub\n---\n\n# Person {n}\n', encoding='utf-8')
        named = everyone if persons is None else persons
        claim = (f'- value: "a {ctype}"\n'
                 f'  id: C-1111111111\n'
                 f'  type: {ctype}\n'
                 f'  persons: [{", ".join(named)}]\n'
                 f'  status: {status}\n  reviewed: 2026-01-01\n'
                 f'  confidence: high\n  date: 1902-04-17\n'
                 + ('  negated: true\n  evidence: negative\n' if negated
                    else '  information: primary\n  evidence: direct\n')
                 + '  notes: x.\n'
                 + roles_block)
        (root / 'sources' / 'notes' / f'rec_{self.SID.lower()}.md').write_text(
            f'---\nid: {self.SID}\ntitle: Rec\nsource_type: vital-record\n---\n\n'
            f'## Claims\n```yaml\n{claim}```\n', encoding='utf-8')
        return root

    def _w126(self, root: Path) -> list:
        from _lib import load_fha_yaml
        findings, _reg = lint._run_lint_core(root, load_fha_yaml(root))
        return [f for f in findings if f.code == 'W126']

    def test_three_person_birth_without_roles_warns(self) -> None:
        # The reporter's shape: a certificate of live birth naming the child
        # and both parents, accepted, and contributing nothing to the pedigree.
        w = self._w126(self._build())
        self.assertEqual(len(w), 1)
        f = w[0]
        self.assertEqual(f.severity, 'W')
        # Says what is wrong, what it costs, and the concrete repair.
        self.assertIn('names 3 people', f.message)
        self.assertIn('fha relate', f.message)
        self.assertIn('child: [P-', f.message)
        self.assertIn('parent: [P-', f.message)
        # The repair must never be "delete the parents from persons:".
        self.assertIn('Leave everyone in persons:', f.message)

    def test_two_person_birth_without_roles_warns(self) -> None:
        # Two people is not a couple-style free pass here: parentage is
        # directed, so the indexer cannot tell the child from the other person
        # and derives nothing. The silence still has to be visible.
        w = self._w126(self._build(persons=[self.CHILD, self.MOTHER]))
        self.assertEqual(len(w), 1)
        self.assertIn('names 2 people', w[0].message)

    def test_roles_map_naming_child_and_parent_is_clean(self) -> None:
        roles = (f'  roles:\n    child: [{self.CHILD}]\n'
                 f'    parent: [{self.FATHER}, {self.MOTHER}]\n')
        self.assertEqual(self._w126(self._build(roles_block=roles)), [])

    def test_single_person_birth_claim_is_clean(self) -> None:
        # The overwhelmingly common birth claim: one person, born. There is no
        # parentage in it to lose, so there is nothing to say.
        self.assertEqual(self._w126(self._build(persons=[self.CHILD])), [])

    def test_child_role_alone_warns_and_asks_for_the_parents(self) -> None:
        roles = f'  roles:\n    child: [{self.CHILD}]\n'
        w = self._w126(self._build(roles_block=roles))
        self.assertEqual(len(w), 1)
        msg = w[0].message
        # The wording has to fit THIS shape: the claim HAS said who was born.
        self.assertIn('says who was born', msg)
        self.assertIn('parent: [P-', msg)

    def test_parent_role_alone_warns_and_asks_who_was_born(self) -> None:
        roles = f'  roles:\n    parent: [{self.FATHER}, {self.MOTHER}]\n'
        w = self._w126(self._build(roles_block=roles))
        self.assertEqual(len(w), 1)
        msg = w[0].message
        self.assertIn('does not say who was born', msg)
        self.assertIn('child: [P-', msg)

    def test_suggested_claim_does_not_warn(self) -> None:
        # Derivation reads accepted claims only, so a suggested claim has cost
        # the tree nothing yet; the repair there is review, which W102 tracks.
        self.assertEqual(self._w126(self._build(status='suggested')), [])

    def test_needs_review_claim_does_not_warn(self) -> None:
        self.assertEqual(self._w126(self._build(status='needs-review')), [])

    def test_negated_claim_does_not_warn(self) -> None:
        # "We researched it and this child was not born to them" (SPEC §8.6).
        # Asking for a roles: map so the tree can record the parentage would be
        # asking the claim to assert what it exists to deny.
        self.assertEqual(self._w126(self._build(negated=True)), [])

    def test_a_marriage_claim_never_draws_this_warning(self) -> None:
        # The false positive this rule must not have: couple claims are W125's
        # business, and a certificate naming six people must not collect a
        # second warning telling it to name a child.
        self.assertEqual(self._w126(self._build(ctype='marriage')), [])

    def test_a_relationship_claim_never_draws_this_warning(self) -> None:
        # A relationship claim missing its roles: map is E015's business - the
        # map is REQUIRED there (SPEC §8.3), which is a different conversation
        # from a birth record that merely could have carried one.
        self.assertEqual(self._w126(self._build(ctype='relationship')), [])

    def test_duplicate_persons_entry_does_not_warn(self) -> None:
        # A bare P-id and a name-link for one child are two entries and one
        # person. There is no parentage to ask about, and counting entries
        # instead of people would demand a parent from a claim naming one baby.
        self.assertEqual(
            self._w126(self._build(persons=[self.CHILD, '"[[Person 0]]"'])), [])

    def test_list_form_roles_does_not_crash_lint(self) -> None:
        # `roles: [child, parent]` names nobody. It cannot scope the parentage
        # (W126 still fires), but it must never raise: a traceback on the
        # human's screen is always a defect (AGENTS.md - "Who you serve").
        w = self._w126(self._build(roles_block='  roles: [child, parent]\n'))
        self.assertEqual(len(w), 1)

    def test_the_message_carries_no_absolute_path(self) -> None:
        # Lint output is quoted into issues and committed reports; a local
        # absolute path has no business in either.
        root = self._build()
        w = self._w126(root)
        self.assertEqual(len(w), 1)
        self.assertNotIn(str(root), w[0].message)


class UnscopedDeathBurialBaptismW132Tests(unittest.TestCase):
    """W132 (#126, reopened): an accepted-or-needs-review death/burial/baptism
    claim naming two or more people with NO `roles:` map at all -
    `_lib.vital_subjects`'s case 2a, where a claim like a burial record naming
    the deceased alongside a grandchild who visited the grave used to read as
    both of their own burials, and now (correctly) reads as neither's. Birth
    has W126 and marriage/divorce have W125 for the equivalent silence;
    death/burial/baptism never had a claim-specific warning at all, so this
    is the third leg of that same table.

    Deliberately narrower than W125/W126 on ROLE SHAPE: fires ONLY on the
    zero-role shape, never when some role IS present but resolves to no
    subject either (`vital_subjects`'s case 5) - that shape answered the
    question, just not in anyone's favor, and stays silent by design.

    Deliberately WIDER than W125/W126 on STATUS AND POLARITY (#173
    follow-up, second round): W125/W126 wait for `accepted, non-negated`
    because they warn about a missing TREE EDGE, which only a claim of that
    shape could ever have created. W132's case 2a is a different problem -
    it is the exact shape `xref.py` puts in its own `unscoped_vital_claim_ids`
    - so it matches `fha xref`'s own scope instead: `accepted` OR
    `needs-review`, and EITHER polarity. A negated claim naming a genuine
    subject and an incidental bystander with no `roles:` map is exactly as
    ambiguous as the positive version of the same shape (negation flips
    whether the event happened, not which named person it happened to), so
    it must get the same `roles:` nudge - see
    `test_negated_claim_warns`/`test_needs_review_claim_warns` below. Only a
    `suggested`, disputed, or rejected claim stays silent, since `fha xref`
    never compares those either.
    """

    A = 'P-d1d1d1d1d1'
    B = 'P-d2d2d2d2d2'
    C = 'P-d3d3d3d3d3'
    SID = 'S-8888888888'

    def _build(self, *, ctype: str = 'death', persons=None,
               roles_block: str = '', status: str = 'accepted',
               negated: bool = False) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / 'people').mkdir(parents=True)
        (root / 'sources' / 'notes').mkdir(parents=True)
        (root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                       encoding='utf-8')
        everyone = [self.A, self.B, self.C]
        for n, pid in enumerate(everyone):
            (root / 'people' / f'x__p{n}_{pid}.md').write_text(
                f'---\nid: {pid}\nname: Person {n}\nsex: U\nliving: false\n'
                f'tier: stub\n---\n\n# Person {n}\n', encoding='utf-8')
        named = everyone if persons is None else persons
        claim = (f'- value: "a {ctype}"\n'
                 f'  id: C-2222222222\n'
                 f'  type: {ctype}\n'
                 f'  persons: [{", ".join(named)}]\n'
                 f'  status: {status}\n  reviewed: 2026-01-01\n'
                 f'  confidence: high\n  date: 1902-04-17\n'
                 + ('  negated: true\n  evidence: negative\n' if negated
                    else '  information: primary\n  evidence: direct\n')
                 + '  notes: x.\n'
                 + roles_block)
        (root / 'sources' / 'notes' / f'rec_{self.SID.lower()}.md').write_text(
            f'---\nid: {self.SID}\ntitle: Rec\nsource_type: vital-record\n---\n\n'
            f'## Claims\n```yaml\n{claim}```\n', encoding='utf-8')
        return root

    def _w132(self, root: Path) -> list:
        from _lib import load_fha_yaml
        findings, _reg = lint._run_lint_core(root, load_fha_yaml(root))
        return [f for f in findings if f.code == 'W132']

    def test_three_person_death_without_roles_warns(self) -> None:
        w = self._w132(self._build(ctype='death'))
        self.assertEqual(len(w), 1)
        f = w[0]
        self.assertEqual(f.severity, 'W')
        self.assertIn('C-2222222222', f.message)
        self.assertIn('names 3 people', f.message)
        self.assertIn('died', f.message)
        self.assertIn('spouse: [P-', f.message)

    def test_two_person_burial_without_roles_warns(self) -> None:
        w = self._w132(self._build(ctype='burial', persons=[self.A, self.B]))
        self.assertEqual(len(w), 1)
        self.assertIn('names 2 people', w[0].message)
        self.assertIn('was buried', w[0].message)

    def test_baptism_without_roles_warns_and_asks_for_a_child_role(self) -> None:
        w = self._w132(self._build(ctype='baptism'))
        self.assertEqual(len(w), 1)
        self.assertIn('names 3 people', w[0].message)
        self.assertIn('who was baptized', w[0].message)
        self.assertIn('child: [P-', w[0].message)

    def test_single_person_death_claim_is_clean(self) -> None:
        # The legacy single-subject shape (vital_subjects case 2, answers
        # None): there is nobody else to be ambiguous about.
        self.assertEqual(self._w132(self._build(persons=[self.A])), [])

    def test_roles_map_that_resolves_a_subject_is_clean(self) -> None:
        # The ordinary shape: the spouse and informant are roled, the
        # deceased is left unroled (case 4) - vital_subjects answers, so
        # there is nothing to warn about.
        roles = f'  roles:\n    spouse: [{self.B}]\n    child: [{self.C}]\n'
        self.assertEqual(self._w132(self._build(roles_block=roles)), [])

    def test_every_person_roled_to_someone_else_does_not_warn(self) -> None:
        # Case 5, not case 2a: SOME role is present and every named person
        # was cast as somebody else - `vital_subjects` answers [] here too,
        # but the claim DID answer the question (just not in favor of anyone
        # it names), and that silence is deliberate, not this warning's
        # business.
        roles = f'  roles:\n    spouse: [{self.A}]\n    child: [{self.B}, {self.C}]\n'
        self.assertEqual(self._w132(self._build(roles_block=roles)), [])

    def test_suggested_claim_does_not_warn(self) -> None:
        # `fha xref` never compares a `suggested` claim either (its own
        # query reads only `accepted`/`needs-review`), so there is nothing
        # yet for a `roles:` map to unblock - review (W102's backlog) comes
        # first.
        self.assertEqual(self._w132(self._build(status='suggested')), [])

    def test_needs_review_claim_warns(self) -> None:
        # #173 follow-up, second round: W132's case 2a now matches
        # `fha xref`'s own scope, which compares BOTH accepted and
        # needs-review claims - unlike W125/W126, which stay accepted-only
        # because they warn about a tree edge only an accepted claim could
        # have created.
        w = self._w132(self._build(status='needs-review'))
        self.assertEqual(len(w), 1)
        self.assertIn('died', w[0].message)

    def test_negated_claim_warns(self) -> None:
        # #173 follow-up, second round: a negated, zero-role, 2+-person claim
        # ("neither A nor B died, contrary to a rumor") has the SAME
        # bystander-vs-joint-subject ambiguity a positive claim of this shape
        # has - it does not say WHICH of them the claim is about, only that
        # the event didn't happen. An earlier version of this fix treated
        # negation as automatically resolving the ambiguity (modeled on how
        # marriage/divorce handles its own counterpart-less negation) and
        # silenced this warning - but a vital event happens to exactly one
        # person, unlike a marriage (a relationship BETWEEN the people it
        # names), so that model does not transfer. This must still warn so a
        # human can add `roles: deceased:` to say who the negation is
        # actually about.
        w = self._w132(self._build(negated=True))
        self.assertEqual(len(w), 1)
        self.assertIn('died', w[0].message)

    def test_negated_needs_review_claim_warns(self) -> None:
        # Both extensions at once: a needs-review, negated, zero-role,
        # 2+-person claim must not fall into the dead end this fix exists to
        # close - excluded from every `fha xref` comparison bucket AND
        # invisible to the one lint check that would tell a human how to fix
        # it.
        w = self._w132(self._build(status='needs-review', negated=True))
        self.assertEqual(len(w), 1)

    def test_negated_claim_with_shared_deceased_role_does_not_warn(self) -> None:
        # A roled negation is not case 2a and must stay silent either way:
        # `roles: deceased:` (SPEC §8.3) names the subject(s) directly, so
        # there is nothing left to disambiguate - mirrors
        # test_roles_map_that_resolves_a_subject_is_clean above, with
        # negated: true added to confirm polarity plays no part once the
        # claim has actually answered the question.
        roles = f'  roles:\n    deceased: [{self.A}, {self.B}]\n'
        self.assertEqual(
            self._w132(self._build(
                persons=[self.A, self.B], roles_block=roles, negated=True)),
            [])

    def test_a_birth_claim_never_draws_this_warning(self) -> None:
        # Birth's equivalent silence is W126's business, not W132's.
        self.assertEqual(self._w132(self._build(ctype='birth')), [])

    def test_a_marriage_claim_never_draws_this_warning(self) -> None:
        # Marriage/divorce's equivalent silence is W125's business.
        self.assertEqual(self._w132(self._build(ctype='marriage')), [])

    def test_duplicate_persons_entry_does_not_warn(self) -> None:
        # A bare P-id and a name-link for one person are two entries and one
        # person - nothing to be ambiguous about.
        self.assertEqual(
            self._w132(self._build(persons=[self.A, '"[[Person 0]]"'])), [])

    def test_list_form_roles_does_not_crash_lint(self) -> None:
        # `roles: [spouse, child]` names nobody - it cannot scope the claim
        # (W132 still fires), but it must never raise.
        w = self._w132(self._build(roles_block='  roles: [spouse, child]\n'))
        self.assertEqual(len(w), 1)

    def test_the_message_carries_no_absolute_path(self) -> None:
        root = self._build()
        w = self._w132(root)
        self.assertEqual(len(w), 1)
        self.assertNotIn(str(root), w[0].message)

    def test_death_impact_text_names_chart_node_and_gedcom(self) -> None:
        # #173 follow-up (post-merge Codex review, finding 2): `death` is the
        # one type among W132's three that genuinely reaches every consumer
        # named here - `gedcom._load_vitals` and `views._build_nodes_bulk`
        # both query `c.type IN ('birth', 'death')`, so a death claim IS read
        # by the GEDCOM writer and the tree's chart nodes once `roles:` scopes
        # it. The full impact list stays accurate for this type.
        w = self._w132(self._build(ctype='death'))
        self.assertEqual(len(w), 1)
        self.assertIn('chart node', w[0].message)
        self.assertIn('GEDCOM', w[0].message)
        self.assertIn('summary box', w[0].message)
        self.assertIn('WikiTree profile', w[0].message)

    def test_burial_impact_text_omits_chart_node_and_gedcom(self) -> None:
        # The false-diagnosis Codex flagged: `gedcom._load_vitals` and
        # `views._build_nodes_bulk` both hard-code `c.type IN ('birth',
        # 'death')`, so a burial claim is never read by either one, roles:
        # or no roles:. Promising the owner that adding `roles:` will restore
        # a chart-node date or a GEDCOM BIRT/DEAT is a repair her fix cannot
        # deliver - the impact text for burial must not name either consumer.
        w = self._w132(self._build(ctype='burial', persons=[self.A, self.B]))
        self.assertEqual(len(w), 1)
        self.assertNotIn('chart node', w[0].message)
        self.assertNotIn('GEDCOM', w[0].message)
        self.assertIn('summary box', w[0].message)
        self.assertIn('WikiTree profile', w[0].message)

    def test_baptism_impact_text_omits_chart_node_and_gedcom(self) -> None:
        # Same false-diagnosis as burial, for baptism - the type Codex's
        # example named explicitly.
        w = self._w132(self._build(ctype='baptism'))
        self.assertEqual(len(w), 1)
        self.assertNotIn('chart node', w[0].message)
        self.assertNotIn('GEDCOM', w[0].message)
        self.assertIn('summary box', w[0].message)
        self.assertIn('WikiTree profile', w[0].message)

    def test_needs_review_impact_text_promises_attribution_not_display(
            self) -> None:
        # #173 follow-up, third round: a genealogist who follows this
        # warning on a needs-review claim and adds `roles:` gets
        # attribution - the claim becomes comparable in `fha xref` - but NOT
        # a summary box, chart node, GEDCOM, or WikiTree entry, since every
        # one of those consumers independently requires `accepted`. The old
        # unconditional wording promised the display outcome even here;
        # this checks the branch that stops making that promise.
        w = self._w132(self._build(status='needs-review'))
        self.assertEqual(len(w), 1)
        self.assertIn('not attributable to anyone yet', w[0].message)
        self.assertIn('fha xref', w[0].message)
        self.assertIn('once accepted', w[0].message)

    def test_negated_impact_text_says_a_negated_claim_never_displays(
            self) -> None:
        # An accepted-but-negated claim has the same problem for a different
        # reason: `xref.py`/`gedcom.py`/`views.py` all require a positive
        # claim, so no amount of accepting will ever put a negated claim in
        # front of any of those consumers. The wording must say so plainly
        # rather than dangling an "once accepted" that this claim can never
        # satisfy.
        w = self._w132(self._build(negated=True))
        self.assertEqual(len(w), 1)
        self.assertIn('not attributable to anyone yet', w[0].message)
        self.assertIn('a negated claim never does, by design', w[0].message)

    def test_needs_review_negated_impact_text_leads_with_negation(
            self) -> None:
        # Both extensions at once: negation, not review status, is the
        # reason display will never happen, so the negated wording must win
        # even though the claim is also needs-review.
        w = self._w132(self._build(status='needs-review', negated=True))
        self.assertEqual(len(w), 1)
        self.assertIn('a negated claim never does, by design', w[0].message)
        self.assertNotIn('once accepted and not negated', w[0].message)


class OrphanedRoleTargetW133Tests(unittest.TestCase):
    """W133 (#126 review, #173 follow-up): a `roles:` value that resolves to a
    real person absent from the claim's own `persons:` list.

    `_lib.resolve_claim_persons_with_roles` (and index.py's `_index_source`,
    its mirror) only ever walks `persons:` entries and asks each one whether
    some role names it - so a role target who never appears in `persons:` is
    not read as a secret extra participant, it is simply never looked at, and
    the pair is dropped before it reaches `vital_subjects`/`spouse_parties`/
    `parentage_parties` at all. Most role words absorb that silently (a broken
    map, not a hidden extra parent). `roles: deceased:` does not: dropping the
    target can leave the claim's only OTHER named person - a widow, say - as
    the sole unroled name left, and `vital_subjects`'s single-person legacy
    fallback then reads HER as the one who died. That is the exact #126 bug
    reintroduced through the hand-edit mistake `roles: deceased:` itself
    invites (write the roles: line, forget the persons: line), and this is
    the check that catches it.
    """

    WIDOW = 'P-d4d4d4d4d4'
    DEAD = 'P-d5d5d5d5d5'
    OTHER = 'P-d6d6d6d6d6'
    SID = 'S-9999999999'

    def _build(self, *, ctype: str = 'death', persons=None,
               roles_block: str = '', status: str = 'accepted',
               claim_id: str = 'C-3333333333') -> Path:
        root = Path(tempfile.mkdtemp())
        (root / 'people').mkdir(parents=True)
        (root / 'sources' / 'notes').mkdir(parents=True)
        (root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                       encoding='utf-8')
        everyone = [self.WIDOW, self.DEAD, self.OTHER]
        for n, pid in enumerate(everyone):
            (root / 'people' / f'x__p{n}_{pid}.md').write_text(
                f'---\nid: {pid}\nname: Person {n}\nsex: U\nliving: false\n'
                f'tier: stub\n---\n\n# Person {n}\n', encoding='utf-8')
        named = [self.WIDOW] if persons is None else persons
        claim = (f'- value: "a {ctype}"\n'
                 f'  id: {claim_id}\n'
                 f'  type: {ctype}\n'
                 f'  persons: [{", ".join(named)}]\n'
                 f'  status: {status}\n  reviewed: 2026-01-01\n'
                 f'  confidence: high\n  date: 1902-04-17\n'
                 '  information: primary\n  evidence: direct\n'
                 '  notes: x.\n'
                 + roles_block)
        (root / 'sources' / 'notes' / f'rec_{self.SID.lower()}.md').write_text(
            f'---\nid: {self.SID}\ntitle: Rec\nsource_type: vital-record\n---\n\n'
            f'## Claims\n```yaml\n{claim}```\n', encoding='utf-8')
        return root

    def _w133(self, root: Path) -> list:
        from _lib import load_fha_yaml
        findings, _reg = lint._run_lint_core(root, load_fha_yaml(root))
        return [f for f in findings if f.code == 'W133']

    def test_deceased_role_naming_someone_outside_persons_warns(self) -> None:
        # Codex's exact reproduction: persons: [widow] only, roles: {deceased:
        # [dead]} - a real, existing P-id, just never added to persons:.
        roles = f'  roles:\n    deceased: [{self.DEAD}]\n'
        w = self._w133(self._build(persons=[self.WIDOW], roles_block=roles))
        self.assertEqual(len(w), 1)
        f = w[0]
        self.assertEqual(f.severity, 'W')
        self.assertIn('C-3333333333', f.message)
        self.assertIn('deceased', f.message)
        self.assertIn(self.DEAD, f.message)
        self.assertIn('persons:', f.message)

    def test_the_underlying_derivation_still_misreads_the_survivor(self) -> None:
        # Documents the deliberate lint-only choice (TOOLING W133): the lint
        # check fires, but `vital_subjects`/`claim_is_own_vital` are left
        # unhardened, because by the time either runs, the orphaned target
        # has already never appeared in `resolve_claim_persons_with_roles`'s
        # output - there is nothing left at that layer to distinguish this
        # claim from a genuinely single-subject one. The lint check is the
        # backstop; this proves it fires exactly where the runtime cannot
        # safely tell the two shapes apart.
        from _lib import resolve_claim_persons_with_roles, vital_subjects
        claim = {
            'id': 'C-3333333333', 'type': 'death',
            'persons': [self.WIDOW],
            'roles': {'deceased': [self.DEAD]},
            'status': 'accepted',
        }
        pairs = resolve_claim_persons_with_roles(claim, alias_map=None)
        self.assertEqual(pairs, [(self.WIDOW.lower(), None)])
        subjects = vital_subjects('death', pairs)
        self.assertIsNone(subjects)   # case 2: reads the widow as the subject

        # ...and the lint check on the SAME claim shape still fires, so the
        # mistake is surfaced even though the runtime derivation cannot see it.
        roles = f'  roles:\n    deceased: [{self.DEAD}]\n'
        w = self._w133(self._build(persons=[self.WIDOW], roles_block=roles))
        self.assertEqual(len(w), 1)

    def test_properly_listed_deceased_role_is_clean(self) -> None:
        # The correct shape: both the widow and the deceased are in persons:.
        roles = f'  roles:\n    deceased: [{self.DEAD}]\n'
        w = self._w133(self._build(
            persons=[self.WIDOW, self.DEAD], roles_block=roles))
        self.assertEqual(w, [])

    def test_orphaned_target_on_a_non_vital_role_also_warns(self) -> None:
        # General hand-edit-integrity check, not specific to deceased: - an
        # orphaned parent: target on a relationship claim is the same mistake.
        roles = f'  roles:\n    child: [{self.WIDOW}]\n    parent: [{self.DEAD}]\n'
        w = self._w133(self._build(
            ctype='relationship', persons=[self.WIDOW], roles_block=roles))
        self.assertEqual(len(w), 1)
        self.assertIn('parent', w[0].message)
        self.assertIn(self.DEAD, w[0].message)

    def test_multiple_orphaned_targets_each_warn(self) -> None:
        roles = (f'  roles:\n    deceased: [{self.DEAD}]\n'
                 f'    witness: [{self.OTHER}]\n')
        w = self._w133(self._build(persons=[self.WIDOW], roles_block=roles))
        self.assertEqual(len(w), 2)

    def test_unresolvable_name_in_roles_is_not_reported_as_orphaned(self) -> None:
        # A typo'd/unknown NAME in roles: never resolves to a P-id at all -
        # that is a different problem (an inert note-link), not this one.
        roles = '  roles:\n    deceased: ["[[Nobody Nowhere]]"]\n'
        w = self._w133(self._build(persons=[self.WIDOW], roles_block=roles))
        self.assertEqual(w, [])

    def test_fires_regardless_of_claim_status(self) -> None:
        # A broken roles:/persons: pairing is a hand-edit mistake the moment
        # it is written - it should surface before review, not only after
        # acceptance (contrast with W125/W126, which wait for accepted, and
        # W132, which waits for accepted-or-needs-review but still not
        # suggested/disputed).
        roles = f'  roles:\n    deceased: [{self.DEAD}]\n'
        for status in ('suggested', 'needs-review', 'accepted', 'disputed'):
            with self.subTest(status=status):
                w = self._w133(self._build(
                    persons=[self.WIDOW], roles_block=roles, status=status))
                self.assertEqual(len(w), 1)

    def test_list_form_roles_does_not_crash_lint(self) -> None:
        # `roles: [spouse, child]` names nobody - it cannot orphan anyone
        # (nothing to resolve), but it must never raise.
        w = self._w133(self._build(roles_block='  roles: [spouse, child]\n'))
        self.assertEqual(w, [])

    def test_the_message_carries_no_absolute_path(self) -> None:
        roles = f'  roles:\n    deceased: [{self.DEAD}]\n'
        root = self._build(persons=[self.WIDOW], roles_block=roles)
        w = self._w133(root)
        self.assertEqual(len(w), 1)
        self.assertNotIn(str(root), w[0].message)


class RootPersonHasChildW127Tests(unittest.TestCase):
    """W127: `root_person` in `fha.yaml` has an accepted genetic child on
    record (issue #70).

    SPEC §12.2 fixes the convention: "#1 = the children, collectively" -
    `root_person` must be anchored at the youngest generation, never at a
    person who has a child on record, or every direct-line couple folder
    derives one generation high while the tree faithfully matches its own
    (wrong) derivation - nothing else catches this, because W110/W119/brackets
    all just verify the folders match the numbers this same walk produces.
    This reuses the exact `children_of` map `_check_ahnentafel_placement`
    already builds for that walk (genetic-only, accepted claims), so the
    check costs nothing extra and can never see an edge the numbering itself
    does not also see.
    """

    ROOT = 'P-4aaaaaaaaa'
    CHILD = 'P-4bbbbbbbbb'
    OTHER = 'P-4ccccccccc'
    SID = 'S-4aaaaaaaaa'

    def _ptext(self, pid: str, name: str, sex: str = 'U') -> str:
        return (f'---\nid: {pid}\nname: {name}\nsex: {sex}\nliving: false\n'
                f'tier: curated\n---\n\n# {name}\n\n## Biography\n\nx\n')

    def _build(self, *, subtype: str = 'biological',
               status: str = 'accepted', root_person: bool = True,
               claim_type: str = 'relationship', negated: bool = False,
               persons: list | None = None, children: list | None = None,
               root_name: str | None = 'Root Person') -> Path:
        """One archive: root_person, a child, a bystander, and one claim.

        The keywords are the axes the derivation rule actually turns on, so
        each test can state exactly one difference from the warning case:
        `claim_type` (a `birth` claim derives parentage too, since #71),
        `negated` (a researched absence derives nothing, SPEC §8.6), `persons`
        (a `roles:` entry naming somebody left out of `persons:` is a broken
        map, not an extra parent), `children` (how many the message counts),
        and `root_name` (None writes a record with an EMPTY `name:`).
        """
        root = Path(tempfile.mkdtemp())
        (root / 'people').mkdir(parents=True)
        (root / 'sources' / 'notes').mkdir(parents=True)
        cfg = f'root_person: {self.ROOT}\n' if root_person else ''
        (root / 'fha.yaml').write_text(
            cfg + 'roots:\n  documents: documents\n', encoding='utf-8')
        root_text = (self._ptext(self.ROOT, root_name, 'M') if root_name
                     else f'---\nid: {self.ROOT}\nname:\nsex: M\nliving: false\n'
                          f'tier: curated\n---\n\n# Root\n\n## Biography\n\nx\n')
        (root / 'people' / f'x__root_{self.ROOT}.md').write_text(
            root_text, encoding='utf-8')
        (root / 'people' / f'x__child_{self.CHILD}.md').write_text(
            self._ptext(self.CHILD, 'Child Person', 'F'), encoding='utf-8')
        (root / 'people' / f'x__other_{self.OTHER}.md').write_text(
            self._ptext(self.OTHER, 'Other Person', 'U'), encoding='utf-8')
        kids = children or [self.CHILD]
        named = persons if persons is not None else kids + [self.ROOT]
        subtype_line = f'  subtype: {subtype}\n' if claim_type == 'relationship' else ''
        negated_lines = '  negated: true\n  evidence: negative\n' if negated else '  evidence: direct\n'
        claim = (
            f'- value: "{kids[0]} child of {self.ROOT}"\n'
            f'  id: C-4aaaaaaaaa\n  type: {claim_type}\n' + subtype_line +
            f'  persons: [{", ".join(named)}]\n  roles:\n'
            f'    child: [{", ".join(kids)}]\n    parent: [{self.ROOT}]\n'
            f'  status: {status}\n  reviewed: 2026-01-01\n  confidence: high\n'
            f'  information: primary\n' + negated_lines + '  notes: x.\n'
        )
        (root / 'sources' / 'notes' / f'rel_{self.SID.lower()}.md').write_text(
            f'---\nid: {self.SID}\ntitle: Rel\nsource_type: other\n---\n\n'
            f'## Claims\n```yaml\n{claim}```\n', encoding='utf-8')
        return root

    def _w127(self, root: Path) -> list:
        from _lib import load_fha_yaml
        findings, _reg = lint._run_lint_core(root, load_fha_yaml(root))
        return [f for f in findings if f.code == 'W127']

    def test_root_person_with_genetic_child_warns(self) -> None:
        w = self._w127(self._build())
        self.assertEqual(len(w), 1)
        f = w[0]
        self.assertEqual(f.severity, 'W')
        self.assertIn(self.ROOT, f.message)
        self.assertIn(self.CHILD, f.message)
        self.assertIn('12.2', f.message)
        self.assertIn('--realign', f.message)
        self.assertEqual(Path(str(f.path)).name, 'fha.yaml')

    def test_root_person_with_no_child_stays_clean(self) -> None:
        # No relationship claim naming ROOT as a parent at all - the
        # ordinary, correctly-anchored archive.
        root = Path(tempfile.mkdtemp())
        (root / 'people').mkdir(parents=True)
        (root / 'sources' / 'notes').mkdir(parents=True)
        (root / 'fha.yaml').write_text(
            f'root_person: {self.ROOT}\nroots:\n  documents: documents\n',
            encoding='utf-8')
        (root / 'people' / f'x__root_{self.ROOT}.md').write_text(
            self._ptext(self.ROOT, 'Root Person', 'M'), encoding='utf-8')
        self.assertEqual(self._w127(root), [])

    def test_adoptive_only_child_does_not_warn(self) -> None:
        # A social/legal-only bond is never numbered into the pedigree (SPEC
        # §12.2), so it must not trip the same warning that protects the
        # numbering - an adoptive parent legitimately anchors the tree.
        self.assertEqual(self._w127(self._build(subtype='adoptive')), [])

    def test_suggested_child_claim_does_not_warn(self) -> None:
        # Derivation (and the Ahnentafel walk itself) reads accepted claims
        # only, so a still-`suggested` child claim has not numbered anything
        # high yet.
        self.assertEqual(self._w127(self._build(status='suggested')), [])

    def test_no_root_person_stays_silent(self) -> None:
        self.assertEqual(self._w127(self._build(root_person=False)), [])

    def test_unresolvable_root_person_stays_silent_on_w127(self) -> None:
        # An unresolvable root_person already gets its own W110 note and the
        # whole Ahnentafel walk is skipped (children_of is never consulted) -
        # W127 must not pile a second, contradictory finding onto the same cause.
        root = Path(tempfile.mkdtemp())
        (root / 'people').mkdir(parents=True)
        (root / 'sources' / 'notes').mkdir(parents=True)
        (root / 'fha.yaml').write_text(
            'root_person: P-9999999999\nroots:\n  documents: documents\n',
            encoding='utf-8')
        self.assertEqual(self._w127(root), [])

    def test_the_message_carries_no_absolute_path(self) -> None:
        root = self._build()
        w = self._w127(root)
        self.assertEqual(len(w), 1)
        self.assertNotIn(str(root), w[0].message)

    def test_negated_child_claim_does_not_warn(self) -> None:
        # A `negated: true` claim is a researched ABSENCE (SPEC §8.6): "we
        # looked and she was not his daughter". `fha index` derives no edge
        # from it, so it numbers nothing high - and telling the human their
        # anchor is wrong on the strength of a claim that denies the bond
        # would be the warning arguing against the archive's own research.
        self.assertEqual(self._w127(self._build(negated=True)), [])

    def test_a_birth_claim_naming_the_parents_warns(self) -> None:
        # Since #71/#82 a birth claim whose roles: map names a child and a
        # parent puts that bond in the pedigree, so it anchors the tree one
        # generation high exactly as a relationship claim does. Reading only
        # `relationship` claims here would leave the hole open in the plainest
        # parentage evidence an archive ever holds.
        w = self._w127(self._build(claim_type='birth'))
        self.assertEqual(len(w), 1)
        self.assertIn(self.CHILD, w[0].message)

    def test_a_role_naming_someone_outside_persons_does_not_warn(self) -> None:
        # `persons:` is who the claim is about (SPEC §8.3), and the indexer
        # builds claim_persons from it - so a roles: entry naming somebody left
        # out of persons: is a broken map, not a secret extra child. Deriving
        # an edge here that `fha index` refuses would put lint one generation
        # out of step with the tree it is describing.
        self.assertEqual(self._w127(self._build(persons=[self.ROOT])), [])

    def test_several_children_are_counted_in_the_message(self) -> None:
        w = self._w127(self._build(children=[self.CHILD, self.OTHER]))
        self.assertEqual(len(w), 1)
        self.assertIn('and 1 more', w[0].message)

    def test_a_nameless_root_person_is_named_by_its_id(self) -> None:
        # `name:` present but empty parses to None. Formatting it would
        # address a person as "None" in a message about her own family, and a
        # bare lowercase p-id would look like a different kind of thing from
        # the P-ids in every other message.
        w = self._w127(self._build(root_name=None))
        self.assertEqual(len(w), 1)
        self.assertNotIn('None', w[0].message)
        self.assertNotIn(self.ROOT.lower(), w[0].message)
        self.assertIn(self.ROOT, w[0].message)

    def test_the_named_fix_reindexes_before_realigning(self) -> None:
        # Editing root_person edits fha.yaml, which is part of the index
        # freshness watermark - so `fha views brackets --realign` refuses with
        # "index is stale" until `fha index` has run. A next step that fails
        # the moment it is followed is a dead end.
        w = self._w127(self._build())
        self.assertEqual(len(w), 1)
        self.assertIn('`fha index` and `fha views brackets --realign`',
                      w[0].message)


class ParentageDerivationParityTests(unittest.TestCase):
    """lint's in-memory parent edges match the ones `fha index` derives.

    `_build_child_edges` is lint's twin of `index.py` `_derive_relationships`,
    and everything shaped by it - W103 bracket lists, the W110/W119/W127
    Ahnentafel walk, E013 summary drift - is only as right as the twin. Read a
    narrower set of claims than the indexer and lint reports a correctly
    written record as broken, then names `fha views brackets --fix` as the
    repair; that command reads the index, so it makes no such change and the
    warning never clears no matter how many times it is run.

    Three rules, each verified here against a claim shape the indexer accepts
    or refuses: `birth` claims derive parentage too (#71), a `negated: true`
    claim derives nothing (SPEC §8.6), and roles are scoped to `persons:`.
    """

    A = 'P-5aaaaaaaaa'      # the parent, and the couple folder's occupant
    B = 'P-5bbbbbbbbb'      # the child
    SID = 'S-5aaaaaaaaa'
    FOLDER = '002 A Person + Spouse []'

    def _ptext(self, pid: str, name: str, sex: str, summary: str = '') -> str:
        return (f'---\nid: {pid}\nname: {name}\nsex: {sex}\nliving: false\n'
                f'tier: curated\n---\n\n# {name}\n\n{summary}## Biography\n\nx\n')

    def _build(self, *, claim_type: str = 'birth', negated: bool = False,
               persons: list | None = None, summary_on_child: bool = False) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / 'people' / self.FOLDER).mkdir(parents=True)
        (root / 'sources' / 'notes').mkdir(parents=True)
        (root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        (root / 'people' / self.FOLDER / f'x__a_{self.A}.md').write_text(
            self._ptext(self.A, 'A Person', 'M'), encoding='utf-8')
        summary = (f'**Parents:** [[{self.A}|A Person]] [[{self.SID}]]\n\n'
                   if summary_on_child else '')
        (root / 'people' / f'x__b_{self.B}.md').write_text(
            self._ptext(self.B, 'B Person', 'F', summary), encoding='utf-8')
        named = persons if persons is not None else [self.B, self.A]
        subtype_line = '  subtype: biological\n' if claim_type == 'relationship' else ''
        negated_lines = ('  negated: true\n  evidence: negative\n' if negated
                         else '  evidence: direct\n')
        claim = (
            f'- value: "B born to A"\n  id: C-5aaaaaaaaa\n'
            f'  type: {claim_type}\n' + subtype_line +
            f'  persons: [{", ".join(named)}]\n  roles:\n'
            f'    child: {self.B}\n    parent: [{self.A}]\n'
            f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
            f'  information: primary\n' + negated_lines + '  notes: x.\n'
        )
        (root / 'sources' / 'notes' / f'rel_{self.SID.lower()}.md').write_text(
            f'---\nid: {self.SID}\ntitle: Birth register\n'
            f'source_type: vital-record\n---\n\n'
            f'## Claims\n```yaml\n{claim}```\n', encoding='utf-8')
        return root

    def _codes(self, root: Path, code: str) -> list:
        from _lib import load_fha_yaml
        findings, _reg = lint._run_lint_core(root, load_fha_yaml(root))
        return [f for f in findings if f.code == code]

    def test_a_birth_claim_fills_the_bracket_list(self) -> None:
        # The folder already names B in its brackets, correctly. Deriving only
        # from `relationship` claims made lint call that list stale and ask for
        # the child to be REMOVED, while `fha views brackets` (reading the
        # index, which does see the birth edge) had nothing to change.
        root = self._build(claim_type='birth')
        folder = root / 'people' / self.FOLDER
        folder.rename(folder.parent / '002 A Person + Spouse [B]')
        self.assertEqual(self._codes(root, 'W103'), [])

    def test_a_negated_claim_stays_out_of_the_bracket_list(self) -> None:
        # The mirror: the folder brackets are empty and must stay empty. A
        # researched absence must never put a child in a couple's folder name.
        # Written as a `relationship` claim on purpose - that is the shape the
        # old derivation DID read, so this pins the negated rule itself rather
        # than passing for free because birth claims were skipped.
        self.assertEqual(
            self._codes(self._build(claim_type='relationship', negated=True),
                        'W103'), [])

    def test_a_role_outside_persons_stays_out_of_the_bracket_list(self) -> None:
        # Same reason for `relationship` here: the persons: scoping rule is
        # what is under test, not the claim type.
        self.assertEqual(
            self._codes(self._build(claim_type='relationship',
                                    persons=[self.A]), 'W103'), [])

    def test_a_birth_claim_backs_a_parents_summary_line(self) -> None:
        # E013 is an ERROR, so this false positive did not merely add noise -
        # it failed the archive's clean-lint gate over a profile whose Parents
        # line cites exactly the evidence the tools tell you to cite.
        root = self._build(claim_type='birth', summary_on_child=True)
        folder = root / 'people' / self.FOLDER
        folder.rename(folder.parent / '002 A Person + Spouse [B]')
        self.assertEqual(self._codes(root, 'E013'), [])
        self.assertEqual(self._codes(root, 'W104'), [])

    def test_a_negated_claim_does_not_back_a_parents_summary_line(self) -> None:
        # The other half of the same rule: a denied bond is not evidence for
        # the Parents line, so the drift is real and E013 still fires.
        root = self._build(claim_type='relationship', negated=True,
                           summary_on_child=True)
        self.assertEqual(len(self._codes(root, 'E013')), 1)


class BracketListBlankChildNameTests(unittest.TestCase):
    """`_check_bracket_lists` (W103) read a candidate bracket child's name as
    `str(registry.person_meta.get(cpid, {}).get('name', ''))` - the '' default
    only fires when the key is missing. A child record with a hand-blanked
    `name:` line (YAML null, key present) str()-converted to the literal
    text 'None' first, which is truthy - so the `if not name: continue`
    skip (meant to drop genuinely nameless children from the derived list)
    never fired, and the derived bracket list carried a literal 'None' as a
    child's name. `fha views brackets --fix` would then rename the couple
    folder to something like `002 A Person + Spouse [None]`."""

    A = 'P-5cccccccc1'   # the parent, and the couple folder's occupant
    B = 'P-5cccccccc2'   # the child - blank name:
    SID = 'S-5cccccccc1'
    FOLDER = '002 A Blank + Spouse []'

    def _build(self) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / 'people' / self.FOLDER).mkdir(parents=True)
        (root / 'sources' / 'notes').mkdir(parents=True)
        (root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        (root / 'people' / self.FOLDER / f'x__a_{self.A}.md').write_text(
            f'---\nid: {self.A}\nname: A Blank\nsex: M\nliving: false\n'
            'tier: curated\n---\n\n# A Blank\n\n## Biography\n\nx\n',
            encoding='utf-8')
        (root / 'people' / f'x__b_{self.B}.md').write_text(
            f'---\nid: {self.B}\nname:\nsex: F\nliving: false\n'
            'tier: curated\n---\n\n# Blank Name Child\n\n## Biography\n\nx\n',
            encoding='utf-8')
        claim = (
            f'- value: "B born to A"\n  id: C-5cccccccc1\n'
            f'  type: birth\n'
            f'  persons: [{self.B}, {self.A}]\n  roles:\n'
            f'    child: {self.B}\n    parent: [{self.A}]\n'
            f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
            f'  information: primary\n  evidence: direct\n  notes: x.\n'
        )
        (root / 'sources' / 'notes' / f'rel_{self.SID.lower()}.md').write_text(
            f'---\nid: {self.SID}\ntitle: Birth register\n'
            f'source_type: vital-record\n---\n\n'
            f'## Claims\n```yaml\n{claim}```\n', encoding='utf-8')
        return root

    def test_a_nameless_child_is_dropped_never_labeled_none(self) -> None:
        from _lib import load_fha_yaml
        root = self._build()
        findings, _reg = lint._run_lint_core(root, load_fha_yaml(root))
        w103 = [f for f in findings if f.code == 'W103']
        # A child with no usable name contributes nothing to the derived
        # bracket list (same as a name-less record already being dropped) -
        # the folder's already-empty brackets match, so nothing is stale.
        for f in w103:
            self.assertNotIn('None', f.message)
        self.assertEqual(w103, [])


class UndecodableSingleFileReadTests(unittest.TestCase):
    """#68, call-shape A: a single required-file read - E009's questions.md
    (line ~831 today) and E018's AGENTS.md (line ~3098 today).

    Both used to be guarded only by `except OSError`, which does not catch
    `UnicodeDecodeError` (a ValueError, not an OSError). A file saved in
    another codepage - cp1252 is what a Windows editor writes by default, and
    the accented names this archive is full of (Krakow, Muller, nee) are
    exactly the bytes that differ from UTF-8 - crashed `fha lint` outright
    instead of being treated as the ordinary "nothing there to check" case a
    missing file already was.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'notes').mkdir(parents=True)
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')

    def _cp1252(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode('cp1252'))
        return p

    def test_questions_md_cp1252_does_not_crash_lint(self) -> None:
        self._cp1252('notes/questions.md',
                     '## Q: Who was she?\n\nGrandma in Kraków, née Müller.\n')
        findings, reg = lint._run_lint_core(self.root, {})
        self.assertEqual(reg.questions_content, '',
                         'an undecodable questions.md reads as "nothing to check", not a crash')

    def test_questions_md_undecodable_is_recorded(self) -> None:
        path = self._cp1252('notes/questions.md', 'Grandma in Kraków.\n')
        _findings, reg = lint._run_lint_core(self.root, {})
        self.assertIn(path, reg.undecodable_files)

    def test_agents_md_cp1252_does_not_crash_and_e018_stays_silent(self) -> None:
        self._cp1252('AGENTS.md', '# Agents\n\nUse fha promote. Grandma in Kraków.\n')
        findings: list = []
        # The OLD 2-arg call shape (on_decode_error defaults to None) - still
        # valid after the fix, so this alone proves the crash is gone.
        lint._check_agent_drift(self.root, findings)
        self.assertEqual(findings, [],
                         'a file that cannot be read has nothing to check for E018 - silent, not a crash')

    def test_agents_md_bytes_are_never_touched(self) -> None:
        path = self._cp1252('AGENTS.md', '# Agents\n\nGrandma in Kraków.\n')
        before = path.read_bytes()
        lint._check_agent_drift(self.root, [])
        self.assertEqual(before, path.read_bytes(),
                         "the file is the human's and is not damaged - never touch it")


class UndecodablePerFileWalkTests(unittest.TestCase):
    """#68, call-shape B: a per-file loop read that must not let one bad file
    cost the rest of the walk - the notes/token-ref sweep (line ~862 today,
    `_walk_archive`) and the GENERATED-header sweep (`_check_generated_headers`,
    line ~3007 today).
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'notes').mkdir(parents=True)
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')

    def _cp1252(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode('cp1252'))
        return p

    def test_notes_walk_cp1252_does_not_crash_lint(self) -> None:
        self._cp1252('notes/bad.md', '# Bad\n\nGrandma in Kraków.\n')
        (self.root / 'notes' / 'good.md').write_text(
            '# Good\n\nfindable words here [H-abcabcabca]\n', encoding='utf-8')
        findings, reg = lint._run_lint_core(self.root, {})
        # One bad note costs only itself - the good note's H-id reference
        # still registers, exactly as index.py's sibling fix keeps indexing
        # the notes that DO decode (#66's "one undecodable note must not cost
        # the notes that DO decode").
        self.assertIn('h-abcabcabca', reg.hypothesis_ids)

    def test_notes_walk_undecodable_file_is_recorded(self) -> None:
        path = self._cp1252('notes/bad.md', 'Grandma in Kraków.\n')
        _findings, reg = lint._run_lint_core(self.root, {})
        self.assertIn(path, reg.undecodable_files)

    def test_generated_headers_cp1252_does_not_crash(self) -> None:
        # Placed outside people/sources/notes so `_lib.read_record` (a
        # DIFFERENT, out-of-scope #68 site - see UndecodableResearchCompanionTests
        # below) never touches this path first: isolates the one line this
        # function owns.
        self._cp1252('stray.md', '# Stray\n\nGrandma in Kraków.\n')
        findings: list = []
        lint._check_generated_headers(self.root, findings)   # old 2-arg call
        self.assertEqual(findings, [])

    def test_generated_headers_still_reads_the_rest_of_the_tree(self) -> None:
        self._cp1252('stray.md', '# Stray\n\nGrandma in Kraków.\n')
        (self.root / 'ok.md').write_text(
            '<!-- GENERATED by fha views -->\n\nbody\n', encoding='utf-8')
        findings: list = []
        # Must not raise, and must still walk past the bad file to ok.md.
        lint._check_generated_headers(self.root, findings)


class GeneratedHeadersSkipsAlreadyWalkedTreesTests(unittest.TestCase):
    """Audit finding: `_check_generated_headers` re-read every file under
    people/sources/notes with its own `archive_root.rglob('*.md')` pass,
    even though Pass 1 (`_walk_archive`) had just finished reading every one
    of those same files moments earlier in the same `_run_lint_core` call -
    and the re-read bought nothing: W105 (the check this function exists
    for) is a deferred no-op (`pass  # deferred: W105 requires mtime
    tracking`), so the second full read of those three trees was pure
    wasted I/O on every `fha lint`/`fha doctor` run, doubling the archive's
    total read cost with zero behavior change.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir(parents=True)
        (self.root / 'notes').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')

    def test_run_lint_core_never_rereads_people_sources_notes_for_generated_headers(self) -> None:
        (self.root / 'people' / 'p1.md').write_text(
            '---\nid: P-aaaaaaaaaa\nname: A\n---\n# A\n', encoding='utf-8')
        (self.root / 'sources' / 's1.md').write_text(
            '---\nid: S-aaaaaaaaaa\ntitle: S\nsource_type: letter\n---\n', encoding='utf-8')
        (self.root / 'notes' / 'n1.md').write_text('# General notes\n', encoding='utf-8')
        seen: list[Path] = []
        real = lint.read_text_or_report

        def spy(path, *a, **kw):
            seen.append(Path(path))
            return real(path, *a, **kw)

        with unittest.mock.patch.object(lint, 'read_text_or_report', side_effect=spy):
            lint._run_lint_core(self.root, {})
        # notes/n1.md is legitimately read once by Pass 1's own FTS/token-ref
        # sweep (_walk_archive, "Notes FTS" section) - that read is not the
        # bug. The bug was _check_generated_headers reading it (and every
        # other file under people/sources/notes) a SECOND time for no
        # benefit, so the real assertion is "no path is read twice", not
        # "these trees are never read via read_text_or_report at all".
        from collections import Counter
        counts = Counter(seen)
        duplicates = {p: n for p, n in counts.items() if n > 1}
        self.assertEqual(duplicates, {},
                         '_check_generated_headers must not re-scan a file Pass 1 already read')

    def test_generated_headers_still_finds_a_stray_root_level_file(self) -> None:
        # The skip is scoped to people/sources/notes specifically - a file
        # living elsewhere (root-level docs, a stray .md) is still Pass 1's
        # blind spot, so _check_generated_headers must still cover it.
        (self.root / 'ok.md').write_text(
            '<!-- GENERATED by fha views -->\n\nbody\n', encoding='utf-8')
        seen: list[Path] = []
        real = lint.read_text_or_report

        def spy(path, *a, **kw):
            seen.append(Path(path))
            return real(path, *a, **kw)

        with unittest.mock.patch.object(lint, 'read_text_or_report', side_effect=spy):
            lint._run_lint_core(self.root, {})
        self.assertIn(self.root / 'ok.md', seen)


class UndecodableFormatCheckTests(unittest.TestCase):
    """#68, call-shape C: the `--format-check` / `--format-write` read
    (`_check_format`, line ~3152 today).

    Only `_check_format` (the READ side) is in scope here. `--format-write`'s
    actual write path, `_fix_format`, reads through `_lib.read_text_exact` -
    a different call shape (`.open('r', encoding='utf-8', newline='')`, not
    `read_text(encoding='utf-8')`) that the issue's grep sweep never matched,
    and it is NOT touched by this change - see the PR body.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')

    def _cp1252(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode('cp1252'))
        return p

    def test_format_check_cp1252_does_not_crash(self) -> None:
        # No final newline and CRLF-worthy content - if this were read, W109
        # would fire twice. It must not be read at all.
        path = self._cp1252('stray.md', 'Grandma in Kraków.')
        findings: list = []
        lint._check_format(path, findings)   # old 2-arg call
        self.assertEqual(findings, [],
                         'an undecodable file was never read, so W109 has nothing to report')

    def test_format_check_via_run_lint_does_not_crash(self) -> None:
        self._cp1252('stray.md', 'Grandma in Kraków.')
        result = lint.run_lint(self.root, {}, format_check=True)
        self.assertNotIn('W109', [m.code for m in result.messages])
        self.assertIn('W128', [m.code for m in result.messages])


class UndecodablePersonAndSourceRecordTests(unittest.TestCase):
    """#68 site 2 and the read that used to mask it: a PERSON or SOURCE record
    whose bytes are not UTF-8.

    Every record in the archive is read through `_lib.read_record`, whose own
    `except OSError`-only guard let `UnicodeDecodeError` (a ValueError) out -
    so `_process_person_file` crashed on a cp1252 person file before the
    research-companion read below it was ever reached, and `fha lint` still
    died on the single most common kind of file in the archive. `read_record`
    now reports the decode through the caller's recorder and hands back
    `undecodable: True`; lint skips the record on that flag rather than
    linting an empty one, so the file earns exactly one W128 instead of a
    cascade of "missing id" / "filename disagrees" errors invented out of
    bytes nobody read.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        self.path = self.root / 'people' / 'x__anne_research_P-3333333333.md'
        self.path.write_bytes(
            ('---\nid: P-3333333333\ncreated: 2026-01-01\n---\n\n'
             '## Open Questions\n\nGrandma in Kraków.\n').encode('cp1252'))

    def test_an_undecodable_person_file_does_not_crash_lint(self) -> None:
        _findings, reg = lint._run_lint_core(self.root, {})
        self.assertIn(self.path, reg.undecodable_files)

    def test_the_companion_read_does_not_enter_the_e009_research_scope(self) -> None:
        _findings, reg = lint._run_lint_core(self.root, {})
        self.assertEqual(list(reg.research_content), [],
                         'an undecodable companion must not enter the E009 research scope')

    def test_no_errors_are_invented_out_of_bytes_nobody_read(self) -> None:
        findings, _reg = lint._run_lint_core(self.root, {})
        offenders = [f for f in findings
                     if f.severity == 'E' and f.path == self.path]
        self.assertEqual(offenders, [],
                         'a file that was never read has no spec violations to report')

    def test_the_file_earns_exactly_one_w128(self) -> None:
        result = lint.run_lint(self.root, {})
        w128 = [m for m in result.messages if m.code == 'W128']
        self.assertEqual(len(w128), 1)
        self.assertIn('x__anne_research_P-3333333333.md', w128[0].text)

    def test_an_undecodable_source_file_is_reported_the_same_way(self) -> None:
        src = self.root / 'sources' / 'letter_S-4444444444.md'
        src.write_bytes(
            ('---\nid: S-4444444444\ntitle: Lettre de Kraków\n---\n\n'
             '## Claims\n\n```yaml\n[]\n```\n').encode('cp1252'))
        result = lint.run_lint(self.root, {})
        w128 = [m for m in result.messages
                if m.code == 'W128' and 'letter_S-4444444444.md' in m.text]
        self.assertEqual(len(w128), 1)

    def test_the_record_is_never_rewritten(self) -> None:
        before = self.path.read_bytes()
        lint.run_lint(self.root, {})
        self.assertEqual(before, self.path.read_bytes())

    def test_one_undecodable_record_does_not_cost_the_others(self) -> None:
        good = self.root / 'people' / 'doe__jane_P-1111111111.md'
        good.write_text(_PERSON_MD, encoding='utf-8')
        _findings, reg = lint._run_lint_core(self.root, {})
        self.assertIn('p-1111111111', reg.all_record_ids)

    def test_the_archive_around_the_skipped_record_still_resolves(self) -> None:
        # The skipped record's ID is read off its FILENAME (SPEC §13 puts it
        # there too, and the filename is bytes this pass CAN read), so a link
        # to her is not reported as an orphan reference to a person who is
        # sitting right there on disk.
        other = self.root / 'people' / 'doe__jane_P-1111111111.md'
        other.write_text(
            _PERSON_MD.replace('## Biography',
                               '## Biography\n\nSister of [[P-3333333333]].'),
            encoding='utf-8')
        findings, _reg = lint._run_lint_core(self.root, {})
        invented = [f for f in findings if f.code in ('E004', 'E005')]
        self.assertEqual(invented, [],
                         'a record nobody could read is not a record that is missing')

    def test_the_id_is_claimed_without_inventing_a_person_record(self) -> None:
        # The ID resolves, but the record's CONTENT is still absent - so no
        # check that needs its frontmatter runs over an empty stand-in.
        _findings, reg = lint._run_lint_core(self.root, {})
        self.assertTrue(reg.has_person('P-3333333333'))
        self.assertNotIn('p-3333333333', reg.person_meta)
        self.assertNotIn('p-3333333333', reg.person_profile_paths)


class UndecodableFormatWriteTests(unittest.TestCase):
    """#68, the write half of the `--format-write` loop (`_fix_format`).

    `_check_format` (the read half) skips an undecodable file; `_fix_format`
    runs on the very next line of the same loop over the same path, through
    `_lib.read_text_exact` - a different call shape the issue's grep sweep
    never matched. Guarding only the read half left `fha lint --format-write`
    crashing on exactly the file the report had just learned to describe.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        self.path = self.root / 'stray.md'
        # No final newline: `--format-write` WOULD rewrite this file if it
        # could read it, which is what makes the skip worth pinning.
        self.path.write_bytes('Grandma in Kraków.'.encode('cp1252'))

    def test_format_write_does_not_crash(self) -> None:
        result = lint.run_lint(self.root, {}, format_write=True)
        self.assertIn('W128', [m.code for m in result.messages])

    def test_format_write_never_rewrites_bytes_it_could_not_read(self) -> None:
        before = self.path.read_bytes()
        lint.run_lint(self.root, {}, format_write=True)
        self.assertEqual(before, self.path.read_bytes())

    def test_format_write_still_fixes_the_files_it_can_read(self) -> None:
        good = self.root / 'ok.md'
        good.write_text('no final newline', encoding='utf-8')
        lint.run_lint(self.root, {}, format_write=True)
        self.assertEqual(good.read_text(encoding='utf-8'), 'no final newline\n')


class SpawnQuestionsRefsTests(unittest.TestCase):
    """#55: `--spawn-questions` wrote the E009 error text itself as the `## Q:`
    heading (a question log telling its reader to run the command that just
    wrote it) and always left `refs: []` even though the two contradicting
    C-ids were right there in the message. The fix must phrase a real
    question using each claim's own `value:` and populate `refs:` with both
    ids - the issue's own regression-test spec: refs contains both C-ids, and
    the heading does not contain the substring `--spawn-questions`.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir()
        (self.root / 'notes').mkdir()
        (self.root / 'people' / 'rivera__sam_P-1111111111.md').write_text(
            _NAMED_PERSON, encoding='utf-8')
        (self.root / 'sources' / 'test_S-1111111111.md').write_text(
            '---\nid: S-1111111111\ntitle: t\nsource_type: other\n---\n\n'
            '## Claims\n```yaml\n'
            '- id: C-1111111111\n  type: birth\n  persons: [P-1111111111]\n'
            '  value: "born 1885"\n  status: accepted\n  confidence: medium\n'
            '  contradicts: [C-2222222222]\n'
            '- id: C-2222222222\n  type: birth\n  persons: [P-1111111111]\n'
            '  value: "born 1886"\n  status: accepted\n  confidence: medium\n'
            '```\n', encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_spawned_question_has_refs_and_a_real_heading(self) -> None:
        result = lint.run_lint(self.root, {}, spawn_questions=True)
        self.assertIn(str(self.root / 'notes' / 'questions.md'), result.changed)
        q = (self.root / 'notes' / 'questions.md').read_text(encoding='utf-8')
        # The issue's own regression-test spec, verbatim: refs contains both
        # C-ids, and the heading does not echo the --spawn-questions error.
        self.assertIn('refs: [C-1111111111, C-2222222222]', q)
        heading_line = [ln for ln in q.splitlines() if ln.startswith('## Q:')][0]
        self.assertNotIn('--spawn-questions', heading_line)
        # The claims' own value: text makes a far better heading than the
        # error message - both positions of the disagreement are visible
        # without opening the source.
        self.assertIn('born 1885', heading_line)
        self.assertIn('born 1886', heading_line)

    def test_spawned_question_satisfies_e009_on_relint(self) -> None:
        lint.run_lint(self.root, {}, spawn_questions=True)
        findings, _ = lint._run_lint_core(self.root, {})
        self.assertEqual([f for f in findings if f.code == 'E009'], [])


class UndecodableQuestionsSpawnTests(unittest.TestCase):
    """#68 in a WRITE path: `--fix` appending E009 contradiction questions.

    `_fix_spawn_questions` rewrites notes/questions.md whole, so its read had
    to be guarded in the one way that does not lose data: refuse. An
    unguarded read crashed here, and a read that fell back to `''` would have
    traded every question the human ever logged for the newly spawned ones -
    unattended, under `--fix`.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'notes').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        self.questions = self.root / 'notes' / 'questions.md'
        self.questions.write_bytes(
            '# Questions\n\n## Q: Where was Kraków?\n'.encode('cp1252'))

    def _run(self) -> tuple[list[str], list[str]]:
        progress: list[str] = []
        changed: list[str] = []
        finding = lint.Finding('E', 'E009', self.root / 'x.md', 'a contradiction')
        lint._fix_spawn_questions(
            lint.Registry(self.root, {}), [finding], self.root, progress, changed)
        return progress, changed

    def test_the_existing_question_log_is_never_clobbered(self) -> None:
        before = self.questions.read_bytes()
        self._run()
        self.assertEqual(before, self.questions.read_bytes(),
                         'a read failure must never turn an append into a truncation')

    def test_the_refusal_is_reported_and_names_the_fix(self) -> None:
        progress, changed = self._run()
        self.assertEqual(changed, [])
        self.assertTrue(any('questions.md' in line and 'UTF-8' in line
                            for line in progress), progress)


class UndecodableFileReportingTests(unittest.TestCase):
    """W128 (#68): the aggregated report over every file `fha lint` could not
    decode as UTF-8 this run - the file-level twin of W123
    (`_check_unreadable_dirs`)."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'notes').mkdir(parents=True)
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')

    def _cp1252(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode('cp1252'))
        return p

    def test_run_lint_reports_w128_and_moves_off_exit_zero(self) -> None:
        self._cp1252('notes/questions.md', '# Q\n\nGrandma in Kraków.\n')
        result = lint.run_lint(self.root, {})
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        w128 = [m for m in result.messages if m.code == 'W128']
        self.assertEqual(len(w128), 1)
        self.assertIn('notes/questions.md', w128[0].text)
        self.assertIn('UTF-8', w128[0].text)

    def test_the_message_carries_no_absolute_path(self) -> None:
        self._cp1252('notes/questions.md', 'Grandma in Kraków.\n')
        result = lint.run_lint(self.root, {})
        w128 = [m for m in result.messages if m.code == 'W128']
        self.assertEqual(len(w128), 1)
        self.assertNotIn(str(self.root), w128[0].text)

    def test_the_file_is_never_rewritten(self) -> None:
        path = self._cp1252('notes/questions.md', 'Grandma in Kraków.\n')
        before = path.read_bytes()
        lint.run_lint(self.root, {})
        self.assertEqual(before, path.read_bytes(),
                         'the note is never rewritten - only its encoding is wrong, not its content')

    def test_a_file_touched_by_more_than_one_pass_earns_one_warning(self) -> None:
        # AGENTS.md sits directly under archive_root, so it is read once by
        # _check_generated_headers's rglob (site 4) and once by
        # _check_agent_drift (site 5) - the de-duplication
        # `undecodable_file_recorder` promises must hold across both.
        self._cp1252('AGENTS.md', '# Agents\n\nGrandma in Kraków.\n')
        result = lint.run_lint(self.root, {})
        w128 = [m for m in result.messages
                if m.code == 'W128' and 'AGENTS.md' in m.text]
        self.assertEqual(len(w128), 1)

    def test_run_lint_silent_also_counts_the_warning(self) -> None:
        # fha doctor's embedded lint summary must not silently omit this.
        self._cp1252('notes/questions.md', 'Grandma in Kraków.\n')
        n_errors, n_warnings, _e018 = lint.run_lint_silent(self.root, {})
        self.assertEqual(n_errors, 0)
        self.assertGreaterEqual(n_warnings, 1)

    def test_a_clean_archive_stays_clean(self) -> None:
        (self.root / 'notes' / 'fine.md').write_text('# Fine\n\nwords\n', encoding='utf-8')
        result = lint.run_lint(self.root, {})
        self.assertNotIn('W128', [m.code for m in result.messages])


class LintStdoutIsValidUtf8Tests(unittest.TestCase):
    """Issue #64: `fha lint`'s stdout must be valid UTF-8 even when the
    interpreter's default stdout encoding is the Windows locale codepage
    (cp1252), the way an unmodified console/redirect defaults on Windows.

    `lint.py` never called `_lib.configure_utf8_stdout()` (every other tool
    that prints non-ASCII does - doctor.py, process.py, ...), so on a cp1252
    machine `fha lint --root . > out.txt` wrote mojibake into the file: the
    issue's own repro is W125's message text, which embeds a literal
    ellipsis in the fixed string `` `spouse: [P-…, P-…]` `` - cp1252 encodes
    U+2026 as the single byte 0x85, which is not valid UTF-8 on its own, so
    a downstream UTF-8 reader (this whole toolchain) chokes or mojibakes.

    This spawns a REAL subprocess rather than asserting in-process, because
    the bug is about the encoding the interpreter binds to `sys.stdout` at
    startup - something no in-process capture (`redirect_stdout`, a StringIO
    swap) can reproduce. `PYTHONIOENCODING=cp1252` pins that startup
    encoding portably (this suite need not run on Windows to prove the
    issue), and `configure_utf8_stdout()`'s job is to override it before any
    output happens.
    """

    def _build_archive_with_w125(self) -> Path:
        """A marriage claim naming 3 distinct people with no `roles:` map -
        the unconditional shape that fires W125 (see
        UnscopedCoupleClaimW125Tests above), so the ellipsis-bearing message
        is guaranteed to print, not merely possible."""
        root = Path(tempfile.mkdtemp())
        (root / 'people').mkdir(parents=True)
        (root / 'sources' / 'notes').mkdir(parents=True)
        (root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                        encoding='utf-8')
        people = ['P-h1h1h1h1h1', 'P-w2w2w2w2w2', 'P-f3f3f3f3f3']
        for n, pid in enumerate(people):
            (root / 'people' / f'x__p{n}_{pid}.md').write_text(
                f'---\nid: {pid}\nname: Person {n}\nsex: U\nliving: false\n'
                f'tier: stub\n---\n\n# Person {n}\n', encoding='utf-8')
        claim = (
            '- value: "a marriage"\n'
            '  id: C-1111111111\n'
            '  type: marriage\n'
            f'  persons: [{", ".join(people)}]\n'
            '  status: accepted\n  reviewed: 2026-01-01\n'
            '  confidence: high\n  date: 1890\n'
            '  information: primary\n  evidence: direct\n'
            '  notes: x.\n'
        )
        (root / 'sources' / 'notes' / 'rec_s-7777777777.md').write_text(
            '---\nid: S-7777777777\ntitle: Rec\nsource_type: vital-record\n---\n\n'
            f'## Claims\n```yaml\n{claim}```\n', encoding='utf-8')
        return root

    def test_w125_ellipsis_survives_redirected_stdout_as_valid_utf8(self) -> None:
        root = self._build_archive_with_w125()
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'cp1252'  # the Windows-default this bug needs
        proc = subprocess.run(
            [sys.executable, str(ROOT / 'tools' / 'lint.py'), '--root', str(root)],
            capture_output=True, check=False, env=env,
        )

        # Sanity: the fixture actually reaches the W125 code path (decode
        # loosely here just to read the sanity check itself).
        stdout_lossy = proc.stdout.decode('cp1252')
        self.assertIn('W125', stdout_lossy, 'fixture did not fire W125 - '
                       f'nothing to reproduce the issue with.\n{stdout_lossy}')

        try:
            decoded = proc.stdout.decode('utf-8')
        except UnicodeDecodeError as e:
            self.fail(
                'lint stdout was not valid UTF-8 under a cp1252 default '
                f'stdout encoding (issue #64): {e}. Raw bytes near the '
                f'failure: {proc.stdout[max(0, e.start - 8):e.start + 8]!r} '
                '- byte 0x85 there is cp1252\'s ellipsis, mis-emitted '
                'instead of the UTF-8 encoding of U+2026.'
            )
        else:
            self.assertIn('…', decoded)  # the ellipsis survived intact


class MissingSectionW130Tests(unittest.TestCase):
    """W130 (#75/#76): a CURATED person record missing one of SPEC §16's four
    hand-written sections. Curated-tier only (a stub's sections are a
    legitimate research backlog, SPEC §4 - not a defect); the GENERATED
    `## Sources` region is never checked (no other generated companion's
    presence is lint-mandated either)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _person(self, tier: str, body: str) -> Path:
        path = self.root / 'people' / 'rivera__sam_P-1111111111.md'
        path.write_text(
            f'---\nid: P-1111111111\nname: Sam Rivera\ntier: {tier}\n'
            f'living: false\n---\n\n# Sam Rivera\n\n{body}', encoding='utf-8')
        return path

    _FULL_BODY = (
        '## Biography\nx\n\n## Stories\n*(none yet)*\n\n'
        '## Research Notes\nx\n\n## Friends & Family\n*(none yet)*\n'
    )

    def test_curated_missing_one_section_fires_naming_it(self) -> None:
        body = self._FULL_BODY.replace('## Research Notes\nx\n\n', '')
        self._person('curated', body)
        findings, _ = lint._run_lint_core(self.root, {})
        w130 = [f for f in findings if f.code == 'W130']
        self.assertEqual(len(w130), 1, findings)
        self.assertIn('Research Notes', w130[0].message)
        self.assertNotIn('Biography', w130[0].message)

    def test_curated_missing_several_sections_names_all(self) -> None:
        self._person('curated', '## Biography\nx\n')
        findings, _ = lint._run_lint_core(self.root, {})
        w130 = [f for f in findings if f.code == 'W130']
        self.assertEqual(len(w130), 1, findings)
        for heading in ('Stories', 'Research Notes', 'Friends & Family'):
            self.assertIn(heading, w130[0].message)
        self.assertNotIn('Biography,', w130[0].message)

    def test_curated_with_all_four_sections_is_silent(self) -> None:
        self._person('curated', self._FULL_BODY)
        findings, _ = lint._run_lint_core(self.root, {})
        self.assertEqual([f for f in findings if f.code == 'W130'], [])

    def test_stub_missing_every_section_is_silent(self) -> None:
        # A stub's sections are a legitimate research backlog (SPEC §4), not
        # a lint-worthy gap - checking every stub would turn this into noise
        # across an archive's whole stub population.
        self._person('stub', '')
        findings, _ = lint._run_lint_core(self.root, {})
        self.assertEqual([f for f in findings if f.code == 'W130'], [])

    def test_missing_sources_region_is_never_flagged(self) -> None:
        # The GENERATED ## Sources region's absence just means nobody has run
        # `fha views sources-index` yet - not a defect this check reports.
        self._person('curated', self._FULL_BODY)   # no ## Sources anywhere
        findings, _ = lint._run_lint_core(self.root, {})
        self.assertEqual([f for f in findings if f.code == 'W130'], [])

    def test_research_notes_in_a_separate_companion_does_not_mask_the_profile_gap(self) -> None:
        # Regression guard: registry.person_bodies CONCATENATES the profile
        # with a separate _research companion's body (by design, for E009's
        # hypothesis search) - a companion carrying its OWN ## Research Notes
        # heading must not silently satisfy this check for a PROFILE that
        # never got one. The check must read the profile file directly.
        body = self._FULL_BODY.replace('## Research Notes\nx\n\n', '')
        self._person('curated', body)
        (self.root / 'people' / 'rivera__sam_research_P-1111111111.md').write_text(
            '---\nid: P-1111111111\n---\n\n## Research Notes\n\n'
            '*(working notes)*\n\n## Open Questions\n\n*(none yet)*\n\n'
            '## Hypotheses\n\n*(none yet)*\n\n## Research Log\n\n*(none yet)*\n',
            encoding='utf-8')
        findings, _ = lint._run_lint_core(self.root, {})
        w130 = [f for f in findings if f.code == 'W130']
        self.assertEqual(len(w130), 1, findings)
        self.assertIn('Research Notes', w130[0].message)


class StrayPersonKeywordW131Tests(unittest.TestCase):
    """W131 (#112): a documents-root asset carries an embedded keyword that
    names a known archive person absent from that source's own `people:`
    list - the stray Lightroom-tag scenario. Guarded behind `--with-exif`
    (`with_exif=True`), like E011/E012's photo-side checks it shares one
    exiftool pass with; exiftool itself is never invoked for real - the
    batched-read seam `lint._run_exiftool_keyword_rows` is monkeypatched.
    """

    LISTED = 'P-1111111111'      # in the source's people:
    STRAY = 'P-2222222222'       # named by the keyword, NOT in people:
    SID = 'S-3333333333'

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        (self.root / 'documents').mkdir(parents=True)
        (self.root / 'photos').mkdir(parents=True)
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir(parents=True)

        (self.root / 'people' / f'smith__ken_{self.LISTED}.md').write_text(
            f'---\nid: {self.LISTED}\nname: Ken Smith\ntier: stub\n'
            'living: false\n---\n\n# Ken Smith\n', encoding='utf-8')
        (self.root / 'people' / f'hartley__margaret_{self.STRAY}.md').write_text(
            f'---\nid: {self.STRAY}\nname: Margaret Hartley\ntier: stub\n'
            'living: false\n---\n\n# Margaret Hartley\n', encoding='utf-8')

        self.asset = self.root / 'documents' / f'deed_{self.SID.lower()}.tif'
        self.asset.write_bytes(b'x')
        (self.root / 'sources' / f'deed_{self.SID.lower()}.md').write_text(
            f'---\nid: {self.SID}\ntitle: Deed\nsource_type: land-record\n'
            f'people: [{self.LISTED}]\n'
            f'files:\n  - file: documents/deed_{self.SID.lower()}.tif\n'
            '    role: primary\n---\n\n## Notes\nA deed.\n', encoding='utf-8')

        from _lib import load_fha_yaml
        self.config = load_fha_yaml(self.root)
        self._orig_rows = lint._run_exiftool_keyword_rows

    def tearDown(self) -> None:
        lint._run_exiftool_keyword_rows = self._orig_rows
        self._tmp.cleanup()

    def _fake_rows(self, keywords=None, subject=None):
        keywords = keywords or []
        subject = subject or []
        def _rows(paths):
            self._called_with = list(paths)
            out = []
            for p in paths:
                row = {'SourceFile': str(p)}
                if str(p).endswith('.tif'):
                    row['Keywords'] = list(keywords)
                    row['Subject'] = list(subject)
                out.append(row)
            return out
        return _rows

    def _w131(self, with_exif: bool = True):
        lint._run_exiftool_keyword_rows = self._fake_rows(
            subject=['Margaret Hartley', 'SOURCE: ' + self.SID])
        findings, _reg = lint._run_lint_core(self.root, self.config, with_exif=with_exif)
        return [f for f in findings if f.code == 'W131']

    def test_stray_person_keyword_absent_from_people_list_warns(self) -> None:
        w131 = self._w131()
        self.assertEqual(len(w131), 1, w131)
        msg = w131[0].message
        self.assertIn('Margaret Hartley', msg)
        self.assertIn(self.SID, msg)
        self.assertIn('fha source clear-keyword', msg)

    def test_keyword_naming_a_listed_person_is_silent(self) -> None:
        lint._run_exiftool_keyword_rows = self._fake_rows(subject=['Ken Smith'])
        findings, _reg = lint._run_lint_core(self.root, self.config, with_exif=True)
        self.assertEqual([f for f in findings if f.code == 'W131'], [])

    def test_unresolvable_keyword_text_is_silently_ignored(self) -> None:
        # An ordinary caption word names no archive person at all.
        lint._run_exiftool_keyword_rows = self._fake_rows(subject=['farm', 'reunion'])
        findings, _reg = lint._run_lint_core(self.root, self.config, with_exif=True)
        self.assertEqual([f for f in findings if f.code == 'W131'], [])

    def test_source_marker_keyword_is_never_flagged(self) -> None:
        lint._run_exiftool_keyword_rows = self._fake_rows(
            keywords=['SOURCE: ' + self.SID])
        findings, _reg = lint._run_lint_core(self.root, self.config, with_exif=True)
        self.assertEqual([f for f in findings if f.code == 'W131'], [])

    def test_photos_root_is_out_of_scope(self) -> None:
        # The same stray-name pattern on a PHOTOS-root file must not trip
        # W131 - scope is documents-root only (see lint.py's docstring for
        # why: photos already have their own richer person-association
        # system, tag-person/face_tags/pid-keywords). Only the PHOTO carries
        # the stray text here (the documents-root .tif stays clean), so the
        # only way this test could see a W131 is if scope leaked to photos.
        photo = self.root / 'photos' / f'snap_{self.SID.lower()}.jpg'
        photo.write_bytes(b'y')

        def _rows(paths):
            return [
                {'SourceFile': str(p),
                 'Subject': ['Margaret Hartley'] if str(p).endswith('.jpg') else []}
                for p in paths
            ]
        lint._run_exiftool_keyword_rows = _rows
        findings, _reg = lint._run_lint_core(self.root, self.config, with_exif=True)
        self.assertEqual([f for f in findings if f.code == 'W131'], [])

    def test_without_with_exif_the_check_never_runs(self) -> None:
        lint._run_exiftool_keyword_rows = self._fake_rows(subject=['Margaret Hartley'])
        findings, _reg = lint._run_lint_core(self.root, self.config, with_exif=False)
        self.assertEqual([f for f in findings if f.code == 'W131'], [])
        self.assertFalse(hasattr(self, '_called_with'),
                         'exiftool must not be invoked at all without --with-exif')

    def test_the_suggested_command_is_shell_quoted_not_python_repr(self) -> None:
        # #147 review (P2): the message used Python's repr() (always single
        # quotes) to show the recovery command. cmd.exe does not treat single
        # quotes as an argument delimiter, so on Windows Command Prompt,
        # copying the suggested `fha source clear-keyword ... --keyword
        # '...'` command splits into multiple arguments and argparse rejects
        # it. _lib.shell_quote already solves this for the active shell (used
        # elsewhere in lint.py/report.py for exactly this purpose) - the
        # message must use it instead.
        from _lib import shell_quote
        w131 = self._w131()
        msg = w131[0].message
        text = 'Margaret Hartley'
        quoted = shell_quote(text)
        self.assertIn(f'--keyword {quoted}', msg)
        # #156 review (P1): on POSIX, shlex.quote('Margaret Hartley') and
        # repr('Margaret Hartley') happen to produce the IDENTICAL
        # single-quoted string - there is no way for a message to contain
        # the shell-quoted form WITHOUT also containing that string, so the
        # negative half of this check only has something real to prove on a
        # platform where the two forms actually differ (Windows, where
        # shell_quote switches to cmd.exe/PowerShell-style double quotes).
        # Asserting both unconditionally made this test fail on Linux/macOS
        # even though the underlying fix (use shell_quote, not repr()) is
        # in place.
        if quoted != repr(text):
            self.assertNotIn(f'--keyword {text!r}', msg)

    def test_the_exiftool_scan_is_batched_not_read_in_one_call(self) -> None:
        # #147 review (P2): the pre-fix reader made ONE call to
        # _run_exiftool_keyword_rows over the ENTIRE scan (that function did
        # its own batching internally, invisible to a caller or a
        # monkeypatch), so every batch's raw JSON rows - Keywords/Subject
        # values included - sat in memory for the whole scan before any
        # reduction happened. That is hundreds of MB, or a killed process, on
        # an archive with tens of thousands of heavily-tagged photos. The fix
        # moves batching to the caller (_read_exif_keywords), which calls
        # _run_exiftool_keyword_rows once per <=50-file group and reduces
        # each batch immediately. Create enough documents-root files to force
        # more than one batch and confirm the seam is actually driven that way.
        bulk_dir = self.root / 'documents' / 'bulk'
        bulk_dir.mkdir(parents=True)
        for i in range(60):
            (bulk_dir / f'file{i:03d}.tif').write_bytes(b'x')

        calls: list[list] = []

        def _rows(paths):
            calls.append(list(paths))
            return [{'SourceFile': str(p)} for p in paths]

        lint._run_exiftool_keyword_rows = _rows
        lint._run_lint_core(self.root, self.config, with_exif=True)

        self.assertGreater(len(calls), 1,
                           'a 61-file scan must be read in more than one exiftool call')
        for batch in calls:
            self.assertLessEqual(len(batch), 50,
                                 'each exiftool call must cover at most one batch')


class ReadExifKeywordsMemoryScopeTests(unittest.TestCase):
    """#147 review (P2): `_read_exif_keywords` (the shared exiftool-keyword
    reducer behind E011/E012's SOURCE: check and W131) must retain a file's
    full raw Keywords/Subject values ONLY for documents-root paths - a
    photos-root file's raw keyword text is never read for anything but the
    cheap SOURCE: S-id extraction, so keeping the rest around for tens of
    thousands of heavily-tagged photos would be pure waste. Calls the
    reducer directly (no archive fixture needed); `_run_exiftool_keyword_rows`
    is monkeypatched the same way the W131 tests above patch it.
    """

    def setUp(self) -> None:
        self._orig_rows = lint._run_exiftool_keyword_rows

    def tearDown(self) -> None:
        lint._run_exiftool_keyword_rows = self._orig_rows

    def test_raw_values_are_kept_only_for_documents_root_paths(self) -> None:
        doc_path = Path('documents/deed.tif').resolve()
        photo_path = Path('photos/1900/snap.jpg').resolve()

        def _rows(paths):
            return [
                {'SourceFile': str(doc_path), 'Subject': ['Margaret Hartley']},
                {'SourceFile': str(photo_path),
                 'Subject': ['a whole pile of unrelated Lightroom tags']},
            ]
        lint._run_exiftool_keyword_rows = _rows

        _source_keywords, raw_keywords = lint._read_exif_keywords(
            [doc_path, photo_path], {doc_path})

        self.assertEqual(raw_keywords.get(doc_path), ['Margaret Hartley'])
        self.assertNotIn(photo_path, raw_keywords,
                         "a photos-root file's raw keyword values must never be retained")

    def test_source_ids_are_extracted_for_every_scanned_file_regardless_of_root(self) -> None:
        doc_path = Path('documents/deed.tif').resolve()
        photo_path = Path('photos/1900/snap.jpg').resolve()
        sid = 'S-2b3c4d5e6f'

        def _rows(paths):
            return [
                {'SourceFile': str(doc_path), 'Keywords': [f'SOURCE: {sid}']},
                {'SourceFile': str(photo_path), 'Keywords': [f'SOURCE: {sid}']},
            ]
        lint._run_exiftool_keyword_rows = _rows

        source_keywords, raw_keywords = lint._read_exif_keywords(
            [doc_path, photo_path], {doc_path})

        self.assertEqual(source_keywords.get(doc_path), {sid.lower()})
        self.assertEqual(source_keywords.get(photo_path), {sid.lower()})
        self.assertNotIn(photo_path, raw_keywords)   # not a documents-root path

    def test_batches_are_capped_at_the_batch_size(self) -> None:
        paths = [Path(f'documents/f{i}.tif').resolve() for i in range(125)]
        calls: list[list] = []

        def _rows(batch):
            calls.append(list(batch))
            return [{'SourceFile': str(p)} for p in batch]

        lint._run_exiftool_keyword_rows = _rows
        lint._read_exif_keywords(paths, set(paths))

        self.assertEqual(len(calls), 3)   # 125 files -> 50 + 50 + 25
        for batch in calls:
            self.assertLessEqual(len(batch), lint._KEYWORD_BATCH_SIZE)


class EphemeraPeopleW134Tests(unittest.TestCase):
    """W134 (#191 follow-up, #114): source_type: ephemera requires people: to
    stay strictly empty (SPEC §14). Before this check, a hand-edited (or
    AI-drafted) ephemera source with a non-empty people: list passed lint
    silently, got indexed into source_people, and wrongly surfaced on that
    person's page/packet/timeline - exactly what the ephemera type exists to
    promise never happens.
    """

    LISTED = 'P-1111111111'
    SID = 'S-4444444444'

    def _build(self, root: Path, *, source_type: str = 'ephemera',
               people: str = '') -> None:
        (root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                        encoding='utf-8')
        (root / 'people').mkdir(parents=True, exist_ok=True)
        (root / 'sources').mkdir(parents=True, exist_ok=True)
        (root / 'people' / f'smith__ken_{self.LISTED}.md').write_text(
            f'---\nid: {self.LISTED}\nname: Ken Smith\ntier: stub\n'
            'living: false\n---\n\n# Ken Smith\n', encoding='utf-8')
        people_line = f'people: [{people}]\n' if people else ''
        (root / 'sources' / f'clipping_{self.SID.lower()}.md').write_text(
            f'---\nid: {self.SID}\ntitle: Clipping\nsource_type: {source_type}\n'
            f'{people_line}---\n\n## Notes\nLocal color, names no one.\n',
            encoding='utf-8')

    def _lint(self, root: Path):
        from _lib import load_fha_yaml
        findings, _reg = lint._run_lint_core(root, load_fha_yaml(root))
        return findings

    def test_ephemera_with_a_person_link_warns(self) -> None:
        root = Path(tempfile.mkdtemp())
        self._build(root, source_type='ephemera', people=self.LISTED)
        w134 = [f for f in self._lint(root) if f.code == 'W134']
        self.assertEqual(len(w134), 1)
        self.assertIn('ephemera', w134[0].message)
        self.assertIn(self.LISTED, w134[0].message)
        self.assertIn('SPEC', w134[0].message)

    def test_ephemera_with_empty_people_is_clean(self) -> None:
        root = Path(tempfile.mkdtemp())
        self._build(root, source_type='ephemera', people='')
        w134 = [f for f in self._lint(root) if f.code == 'W134']
        self.assertEqual(w134, [])

    def test_non_ephemera_source_with_a_person_link_is_unaffected(self) -> None:
        # The same people: link on an ordinary source type is exactly the
        # normal, expected shape - W134 must never fire outside ephemera.
        root = Path(tempfile.mkdtemp())
        self._build(root, source_type='newspaper', people=self.LISTED)
        w134 = [f for f in self._lint(root) if f.code == 'W134']
        self.assertEqual(w134, [])

    def test_ephemera_with_an_unresolved_name_link_still_warns(self) -> None:
        # A people: entry that names nobody in the archive still contradicts
        # the strict-empty rule the moment it's written - W134 does not wait
        # for E005/resolution to judge it, unlike the index's own consumption.
        root = Path(tempfile.mkdtemp())
        self._build(root, source_type='ephemera', people='"[[Nobody Registered]]"')
        w134 = [f for f in self._lint(root) if f.code == 'W134']
        self.assertEqual(len(w134), 1)

    def test_the_message_leads_with_retype_not_clear(self) -> None:
        # #191 follow-up, round 4: the message used to offer "clear people:"
        # as an unconditional first option - but per SPEC §14/the FAQ, naming
        # someone genuinely in the archive (even in passing) disqualifies
        # ephemera outright, so clearing the link there would just discard
        # real evidence rather than fix the misclassification. "Clear" is
        # only right for an erroneous or untracked-name entry - the message
        # must lead with retype-and-keep and condition "clear" explicitly,
        # not hand out "clear" as the first-listed, seemingly safe default.
        root = Path(tempfile.mkdtemp())
        self._build(root, source_type='ephemera', people=self.LISTED)
        w134 = [f for f in self._lint(root) if f.code == 'W134']
        self.assertEqual(len(w134), 1)
        message = w134[0].message
        retype_pos = message.index('re-type it')
        clear_pos = message.index('clear people:')
        self.assertLess(retype_pos, clear_pos,
                         'retype-and-keep must be offered before clear people:')
        self.assertIn('genuinely in your archive', message)
        self.assertIn('does not really belong here', message)


if __name__ == '__main__':
    unittest.main()
