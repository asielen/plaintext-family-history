"""
test_stubs.py - unit tests for the _lib.py stub renderers and their stubs.py
wrappers (Task 2, fha-serve plan 17).

`stub_slug_name` / `stub_filename` / `render_stub_content` moved out of
stubs.py into `_lib.py` so a later `fha person new` can share them without
`tools/person.py` importing `tools/stubs.py` (tools never import tools). The
load-bearing guarantees this file checks:
  - every existing `fha stubs` call site still gets byte-identical output
    when the new sex/gender/birth/death keywords are omitted;
  - the new keywords extend the record correctly when given, in the field
    order SPEC §9 expects (id, aliases, name, [sex], [gender], living,
    birth/death, created, tier);
  - `sex` is validated against the SPEC §9 controlled vocabulary;
  - stubs.py's thin wrappers (`_slug_name`/`_stub_filename`/`_stub_content`)
    still work, including `_stub_filename`'s historical (pid, name) argument
    order (the shared `_lib.stub_filename` takes (name, pid) instead).
"""

import contextlib
import datetime
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import stubs
from _lib import (
    PERSON_SEX_VALUES,
    read_record,
    render_person_body_scaffold,
    render_stub_content,
    stub_filename,
    stub_slug_name,
)


def _meta(text: str) -> dict:
    """Parse rendered stub text's frontmatter the way a real stub file is read."""
    fd, p = tempfile.mkstemp(suffix='.md')
    os.close(fd)
    Path(p).write_text(text, encoding='utf-8')
    try:
        return read_record(p)['meta']
    finally:
        os.unlink(p)


# The exact text `stubs._stub_content` produced before this refactor - the
# byte-identical-output contract this whole file guards.
_OLD_DEFAULT_TEMPLATE = (
    '---\n'
    'id: {pid}\n'
    'aliases: [{pid}]\n'
    'name: {name}\n'
    'living: unknown\n'
    '# birth:   # an honest guess is fine - a tool will remind you to add a source later\n'
    '# death:   # same here; leave commented until you know\n'
    'created: {today}\n'
    'tier: stub\n'
    '---\n'
)


class StubSlugNameTests(unittest.TestCase):
    def test_two_word_name(self) -> None:
        self.assertEqual(stub_slug_name('Jane Doe'), ('doe', 'jane'))

    def test_multi_word_given_names(self) -> None:
        self.assertEqual(stub_slug_name('Mary Ann Smith'), ('smith', 'mary_ann'))

    def test_single_word_name_has_no_surname(self) -> None:
        # SPEC §13: a surname-less person (a mononym) leaves the sort-name slot
        # EMPTY, so the filename leads with the double underscore
        # (`__cher_P-….md`) - hence the empty surname slug, not 'unknown'.
        self.assertEqual(stub_slug_name('Cher'), ('', 'cher'))

    def test_empty_string_falls_back_to_unknown(self) -> None:
        self.assertEqual(stub_slug_name(''), ('unknown', 'unknown'))

    def test_whitespace_only_falls_back_to_unknown(self) -> None:
        self.assertEqual(stub_slug_name('   '), ('unknown', 'unknown'))

    def test_punctuation_is_stripped(self) -> None:
        surname, given = stub_slug_name("Mary O'Brien-Smith Jr.")
        self.assertNotIn("'", surname)
        self.assertNotIn('-', given + surname)
        self.assertNotIn('.', given)

    def test_punctuation_is_stripped_from_a_mononym_too(self) -> None:
        # GUARD: the single-token path returned the token unslugged, so the
        # `[a-z0-9_]` promise held for every name EXCEPT a one-word one.
        # `Bob/Rob` filed as `__bob/rob_P-….md` - a path separator inside a
        # filename, aiming the write at a folder that is not there - and a
        # `?` or `:` produced a name Windows refuses outright.
        self.assertEqual(stub_slug_name("O'Brien"), ('', 'obrien'))
        self.assertEqual(stub_slug_name('Bob/Rob'), ('', 'bobrob'))
        self.assertEqual(stub_slug_name('Ka:wehi'), ('', 'kawehi'))
        self.assertEqual(stub_filename('Bob/Rob', 'P-0000000001'),
                         '__bobrob_P-0000000001.md')

    def test_a_mononym_that_sanitises_to_nothing_stays_surname_less(self) -> None:
        # Still a mononym, just an unspellable one: the sort-name slot stays
        # EMPTY (§13) and only the given slot falls back to 'unknown'.
        self.assertEqual(stub_slug_name('?'), ('', 'unknown'))
        self.assertEqual(stub_filename('?', 'P-0000000001'),
                         '__unknown_P-0000000001.md')

    # -- Generational suffixes (issue #53) --------------------------------
    # "Roy Eugene Dodson Jr" used to file as `jr__roy_eugene_dodson_P-….md`,
    # sorting the son under a different letter than his father
    # (`dodson__roy_eugene_P-….md`). A trailing Jr/Sr/II/III/IV/V must never
    # become the surname - it rides at the end of the given slug instead.

    def test_suffix_is_pulled_off_the_surname_slot(self) -> None:
        self.assertEqual(
            stub_slug_name('Roy Eugene Dodson Jr'),
            ('dodson', 'roy_eugene_jr'),
        )

    def test_father_and_son_share_the_same_surname_slug(self) -> None:
        father = stub_slug_name('Roy Eugene Dodson')
        son = stub_slug_name('Roy Eugene Dodson Jr')
        self.assertEqual(father[0], son[0])
        self.assertEqual(father[0], 'dodson')

    def test_every_suffix_in_the_list_round_trips(self) -> None:
        for suffix in ('Jr', 'Jr.', 'Sr', 'Sr.', 'II', 'III', 'IV', 'V'):
            with self.subTest(suffix=suffix):
                surname, given = stub_slug_name(f'James Whitelock {suffix}')
                self.assertEqual(surname, 'whitelock')
                self.assertEqual(given, f'james_{suffix.rstrip(".").lower()}')

    def test_suffix_is_case_insensitive(self) -> None:
        self.assertEqual(stub_slug_name('Roy Dodson jr'), ('dodson', 'roy_jr'))

    def test_two_token_given_plus_suffix_has_no_promoted_surname(self) -> None:
        # "Roy Jr" is no more a real surname than "Roy" alone - the suffix
        # must not promote the remaining word into one (that would just move
        # the original bug from "Jr" to "Roy").
        self.assertEqual(stub_slug_name('Roy Jr'), ('', 'roy_jr'))

    def test_suffix_alone_has_nothing_to_strip_to_reads_as_a_mononym(self) -> None:
        # A name that IS only a suffix has no token left over to carry the
        # name once stripped, so it falls through unchanged to the ordinary
        # single-token/mononym path.
        self.assertEqual(stub_slug_name('Jr'), ('', 'jr'))

    def test_roman_numeral_alone_is_a_plain_mononym(self) -> None:
        # "IV" with nothing else present is just a given name/mononym, not a
        # suffix with nothing to attach to.
        self.assertEqual(stub_slug_name('IV'), ('', 'iv'))

    def test_roman_numeral_as_second_token_reads_as_a_suffix(self) -> None:
        # Documented tradeoff: "IV" after another token is indistinguishable
        # from the generational suffix - the --surname override is the
        # escape hatch when that reading is wrong.
        self.assertEqual(stub_slug_name('John IV'), ('', 'john_iv'))

    def test_true_mononym_is_byte_identical_to_before_the_fix(self) -> None:
        # The suffix fix must not touch the real mononym contract at all -
        # same code path, same output, no new sanitisation applied.
        self.assertEqual(stub_slug_name('Cher'), ('', 'cher'))

    # -- --surname override -------------------------------------------------

    def test_surname_override_replaces_the_automatic_split(self) -> None:
        self.assertEqual(
            stub_slug_name('Maria Jose Garcia Lopez', surname='Garcia Lopez'),
            ('garcia_lopez', 'maria_jose'),
        )

    def test_surname_override_matches_a_leading_surname_first_name(self) -> None:
        self.assertEqual(
            stub_slug_name('Garcia Lopez Maria Jose', surname='Garcia Lopez'),
            ('garcia_lopez', 'maria_jose'),
        )

    def test_surname_override_unrelated_to_name_keeps_the_full_name_as_given(self) -> None:
        # Neither a prefix nor a suffix of the name - never silently drop
        # part of it; a redundant given slug is honest, a guessed deletion
        # is not.
        self.assertEqual(
            stub_slug_name('Mystery Person', surname='Totally Different'),
            ('totally_different', 'mystery_person'),
        )

    def test_surname_override_takes_priority_over_suffix_handling(self) -> None:
        self.assertEqual(
            stub_slug_name('Roy Eugene Dodson Jr', surname='Dodson'),
            ('dodson', 'roy_eugene_jr'),
        )


class StubFilenameTests(unittest.TestCase):
    def test_named_person(self) -> None:
        self.assertEqual(
            stub_filename('Jane Doe', 'P-aaaaaaaaaa'),
            'doe__jane_P-aaaaaaaaaa.md',
        )

    def test_mononym_leads_with_double_underscore(self) -> None:
        # SPEC §13: a real single-token name (a mononym, an enslaved ancestor
        # recorded only by a given name) has an EMPTY sort-name slot, so the
        # filename leads with the double underscore - `__cher_P-….md`, NOT the
        # `unknown__cher_…` a genuinely nameless fallback would use.
        self.assertEqual(
            stub_filename('Cher', 'P-ffffffffff'),
            '__cher_P-ffffffffff.md',
        )

    def test_none_name_uses_surname_less_unknown_form(self) -> None:
        # The double-underscore-only form is SPEC §13's surname-less
        # convention (mononyms, enslaved ancestors named only by a given
        # name) - an unresolved reference should read the same way on disk.
        self.assertEqual(
            stub_filename(None, 'P-bbbbbbbbbb'),
            'unknown__unknown_P-bbbbbbbbbb.md',
        )

    def test_literal_unknown_name_uses_surname_less_form(self) -> None:
        self.assertEqual(
            stub_filename('unknown', 'P-cccccccccc'),
            'unknown__unknown_P-cccccccccc.md',
        )

    def test_blank_name_uses_surname_less_form(self) -> None:
        self.assertEqual(
            stub_filename('', 'P-dddddddddd'),
            'unknown__unknown_P-dddddddddd.md',
        )

    def test_case_insensitive_unknown_sentinel(self) -> None:
        self.assertEqual(
            stub_filename('UNKNOWN', 'P-eeeeeeeeee'),
            'unknown__unknown_P-eeeeeeeeee.md',
        )


class RenderStubContentDefaultOutputTests(unittest.TestCase):
    """No sex/gender/birth/death given: output must match the pre-refactor
    stubs.py behavior byte-for-byte (tests test_alias_layer, test_graduation,
    test_provisional_vitals, test_templates already guard this from the
    stubs.py side; these are the same contract checked at the _lib level)."""

    def test_byte_identical_to_pre_refactor_output(self) -> None:
        text = render_stub_content('P-aaaaaaaaaa', 'Jane Doe')
        expected = _OLD_DEFAULT_TEMPLATE.format(
            pid='P-aaaaaaaaaa', name='Jane Doe',
            today=datetime.date.today().isoformat(),
        )
        self.assertEqual(text, expected)

    def test_unknown_name_renders_as_unknown(self) -> None:
        text = render_stub_content('P-aaaaaaaaaa', None)
        self.assertIn('name: unknown\n', text)

    def test_no_sex_or_gender_line_when_omitted(self) -> None:
        text = render_stub_content('P-aaaaaaaaaa', 'Jane Doe')
        self.assertNotIn('sex:', text)
        self.assertNotIn('gender:', text)

    def test_birth_death_stay_commented_when_omitted(self) -> None:
        text = render_stub_content('P-aaaaaaaaaa', 'Jane Doe')
        self.assertIn('# birth:', text)
        self.assertIn('# death:', text)
        meta = _meta(text)
        self.assertNotIn('birth', meta)
        self.assertNotIn('death', meta)


class RenderStubContentYamlQuotingTests(unittest.TestCase):
    """P2 codex finding (PR #30): `name`/`gender` are free text a human types
    (`fha person new "Baby #2"`) and were spliced into the frontmatter
    unquoted. YAML reads an unquoted ` #` as a comment marker and an
    unquoted `: ` as a new mapping key, so a name carrying either silently
    truncated on read-back, or - for `: ` - could corrupt the record. Both
    fields must route through `yaml_inline` like every other free-text
    frontmatter writer in this codebase."""

    def test_name_with_hash_round_trips_whole(self) -> None:
        text = render_stub_content('P-aaaaaaaaaa', 'Baby #2')
        self.assertIn("name: 'Baby #2'\n", text)
        self.assertEqual(_meta(text)['name'], 'Baby #2')

    def test_name_with_colon_round_trips_whole(self) -> None:
        text = render_stub_content('P-aaaaaaaaaa', 'Twin: firstborn')
        self.assertEqual(_meta(text)['name'], 'Twin: firstborn')

    def test_plain_name_stays_unquoted(self) -> None:
        # No YAML-significant characters - yaml_inline should not add quotes
        # a human didn't ask for (keeps the byte-identical-output contract
        # for the overwhelmingly common case).
        text = render_stub_content('P-aaaaaaaaaa', 'Jane Doe')
        self.assertIn('name: Jane Doe\n', text)

    def test_gender_with_yaml_significant_text_round_trips_whole(self) -> None:
        text = render_stub_content('P-aaaaaaaaaa', 'Jane Doe', gender='non-binary: they/them')
        self.assertEqual(_meta(text)['gender'], 'non-binary: they/them')


class RenderStubContentExtensionTests(unittest.TestCase):
    """The sex/gender/birth/death keywords `fha person new` will use."""

    def test_sex_line_written_when_given(self) -> None:
        text = render_stub_content('P-aaaaaaaaaa', 'Jane Doe', sex='F')
        self.assertIn('sex: F\n', text)
        self.assertEqual(_meta(text)['sex'], 'F')

    def test_every_valid_sex_value_accepted(self) -> None:
        for value in PERSON_SEX_VALUES:
            text = render_stub_content('P-aaaaaaaaaa', 'Jane Doe', sex=value)
            self.assertIn(f'sex: {value}\n', text)

    def test_invalid_sex_is_refused_with_a_plain_message(self) -> None:
        with self.assertRaises(ValueError) as cm:
            render_stub_content('P-aaaaaaaaaa', 'Jane Doe', sex='female')
        message = str(cm.exception)
        # Names the valid values and distinguishes sex from gender - the
        # AGENTS_TOOLING jargon-needs-a-gloss-and-example rule.
        self.assertIn('gender', message)
        for value in sorted(PERSON_SEX_VALUES):
            self.assertIn(value, message)

    def test_gender_is_free_text_and_unvalidated(self) -> None:
        text = render_stub_content('P-aaaaaaaaaa', 'Jane Doe', gender='non-binary')
        self.assertIn('gender: non-binary\n', text)

    def test_birth_written_as_real_line_with_reassurance(self) -> None:
        text = render_stub_content('P-aaaaaaaaaa', 'Jane Doe', birth='1840~')
        self.assertIn(
            'birth: 1840~   # unsourced estimate - a tool will remind you to add a source\n',
            text,
        )
        self.assertNotIn('# birth:', text)
        self.assertEqual(_meta(text)['birth'], '1840~')

    def test_death_written_as_real_line_with_reassurance(self) -> None:
        text = render_stub_content('P-aaaaaaaaaa', 'Jane Doe', death='1923')
        self.assertIn(
            'death: 1923   # unsourced estimate - a tool will remind you to add a source\n',
            text,
        )
        self.assertNotIn('# death:', text)
        # A bare year parses as a YAML int (same as any other unquoted EDTF
        # year elsewhere in the archive) - str() here matches what a real
        # reader (e.g. index.py's str(meta.get('death', ''))) does with it.
        self.assertEqual(str(_meta(text)['death']), '1923')

    def test_birth_given_death_omitted_keeps_death_commented(self) -> None:
        # Each field is decided independently - a stub can carry a real
        # birth: and a still-commented # death:.
        text = render_stub_content('P-aaaaaaaaaa', 'Jane Doe', birth='1840~')
        self.assertIn('birth: 1840~', text)
        self.assertIn('# death:', text)
        meta = _meta(text)
        self.assertEqual(meta['birth'], '1840~')
        self.assertNotIn('death', meta)

    def test_field_order_with_every_option_set(self) -> None:
        text = render_stub_content(
            'P-aaaaaaaaaa', 'Jane Doe',
            sex='F', gender='woman', birth='1840~', death='1923',
        )
        keys = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped in ('---', '') :
                continue
            keys.append(stripped.lstrip('#').strip().split(':', 1)[0])
        self.assertEqual(
            keys,
            ['id', 'aliases', 'name', 'sex', 'gender', 'living',
             'birth', 'death', 'created', 'tier'],
        )

    def test_field_order_matches_default_when_extensions_omitted(self) -> None:
        text = render_stub_content('P-aaaaaaaaaa', 'Jane Doe')
        keys = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped in ('---', ''):
                continue
            keys.append(stripped.lstrip('#').strip().split(':', 1)[0])
        self.assertEqual(
            keys,
            ['id', 'aliases', 'name', 'living', 'birth', 'death', 'created', 'tier'],
        )


class FromNamesGenerationalSuffixTests(unittest.TestCase):
    """`fha stubs --from-names` (`mint_named_stubs`) is the SECOND entry point
    issue #53 confirmed the bug on ("Roy Dodson Jr." -> `jr__roy_dodson_...`).
    It shares `_lib.stub_filename` with `fha person new`, so this exercises
    that shared path from the batch side."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stub_names(self) -> set[str]:
        stubs_dir = self.root / 'people' / 'stubs'
        if not stubs_dir.is_dir():
            return set()
        return {p.name for p in stubs_dir.iterdir()}

    def test_suffix_and_father_file_under_the_same_surname(self) -> None:
        stubs.mint_named_stubs(
            self.root, ['Roy Eugene Dodson', 'Roy Eugene Dodson Jr'])
        names = sorted(self._stub_names())
        self.assertEqual(len(names), 2)
        # Compared as whole name slots, never as a substring of the filename:
        # the minted P-id is 10 random Crockford characters sitting in that
        # same string, so `'jr' in name` also matched the FATHER whenever his
        # id happened to contain those two letters next to each other (about
        # one run in a hundred - `dodson__roy_eugene_p-xnq0gwdjr1.md` is a
        # real CI failure). Substring where a token was meant is the defect
        # this whole issue is about; the test should not repeat it.
        self.assertEqual(
            {n[:-len('.md')].rsplit('_', 1)[0] for n in names},
            {'dodson__roy_eugene', 'dodson__roy_eugene_jr'},
        )

    def test_period_suffix_from_the_reported_repro(self) -> None:
        # The issue's own confirmed reproduction: "Roy Dodson Jr." via
        # --from-names.
        stubs.mint_named_stubs(self.root, ['Roy Dodson Jr.'])
        names = self._stub_names()
        self.assertEqual(len(names), 1)
        filename = next(iter(names))
        self.assertTrue(filename.startswith('dodson__roy_jr_'), filename)

    def test_second_confirmed_repro_name(self) -> None:
        # Also confirmed the same day on "James Whitelock Jr." per the issue.
        stubs.mint_named_stubs(self.root, ['James Whitelock Jr.'])
        names = self._stub_names()
        self.assertEqual(len(names), 1)
        filename = next(iter(names))
        self.assertTrue(filename.startswith('whitelock__james_jr_'), filename)


class StubsModuleWrapperTests(unittest.TestCase):
    """stubs.py's thin private wrappers around the shared _lib functions."""

    def test_slug_name_wrapper(self) -> None:
        self.assertEqual(stubs._slug_name('Jane Doe'), stub_slug_name('Jane Doe'))

    def test_stub_filename_wrapper_keeps_historical_pid_name_order(self) -> None:
        # stubs._stub_filename(pid, name) - note the order is the OPPOSITE of
        # the shared _lib.stub_filename(name, pid); this wrapper exists
        # precisely to keep that historical call shape working.
        self.assertEqual(
            stubs._stub_filename('P-aaaaaaaaaa', 'Jane Doe'),
            stub_filename('Jane Doe', 'P-aaaaaaaaaa'),
        )
        self.assertEqual(
            stubs._stub_filename('P-aaaaaaaaaa', 'Jane Doe'),
            'doe__jane_P-aaaaaaaaaa.md',
        )

    def test_stub_content_wrapper_is_frontmatter_plus_full_body(self) -> None:
        # #75/#76: a stub is no longer frontmatter-only - _stub_content is
        # render_stub_content's frontmatter with render_person_body_scaffold's
        # full body (purpose block, ## Sources placeholder, the four
        # hand-written sections) appended, so `fha stubs` and `fha person new`
        # mint byte-identical records.
        self.assertEqual(
            stubs._stub_content('P-aaaaaaaaaa', 'Jane Doe'),
            render_stub_content('P-aaaaaaaaaa', 'Jane Doe')
            + render_person_body_scaffold('Jane Doe'),
        )

    def test_stub_content_body_carries_the_76_sections(self) -> None:
        content = stubs._stub_content('P-aaaaaaaaaa', 'Jane Doe')
        for heading in ('## Sources', '## Biography', '## Stories',
                        '## Research Notes', '## Friends & Family'):
            self.assertIn(heading, content)
        self.assertIn('# Jane Doe', content)
        self.assertIn("record - yours to write", content)

    def test_stub_content_falls_back_to_unknown_name_for_body_h1(self) -> None:
        # render_stub_content already writes `name: unknown` when name is
        # None (the common auto-minted-from-a-reference case); the body's
        # own H1/purpose block must not crash or silently omit a title.
        content = stubs._stub_content('P-aaaaaaaaaa', None)
        self.assertIn('# unknown', content)


_GOOD_PERSON = '''---
id: P-1111111111
name: Known Person
living: false
---
'''

_GOOD_SOURCE = '''---
id: S-1111111111
title: Test source
source_type: other
---

## Claims
```yaml
- id: C-1111111111
  type: birth
  persons: ["[[P-9999999999|Ghost Person]]"]
  value: born sometime
  status: suggested
  confidence: low
```
'''


class UndecodableFileScanTests(unittest.TestCase):
    """#68: `_collect_unresolved_persons` scans every people/ and sources/
    file to build known-pid and unresolved-pid sets - a whole-archive walk,
    so one file saved in another encoding (cp1252, a Windows editor's
    default) must not crash the whole `fha stubs` run. Both loops (people/
    at line ~78, sources/ at line ~96) share this one test class because
    they are the same shape: skip the bad file, keep scanning, report once.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people').mkdir(parents=True)
        (self.root / 'sources').mkdir(parents=True)
        (self.root / 'people' / 'known__person_P-1111111111.md').write_text(
            _GOOD_PERSON, encoding='utf-8')
        (self.root / 'sources' / 'test_S-1111111111.md').write_text(
            _GOOD_SOURCE, encoding='utf-8')
        # One undecodable file in EACH loop - a person record and a source
        # record, both saved as cp1252 rather than UTF-8.
        (self.root / 'people' / 'muller__anne_P-2222222222.md').write_bytes(
            ('---\nid: P-2222222222\nname: Anne Müller\nliving: false\n---\n'
             ).encode('cp1252'))
        (self.root / 'sources' / 'krakow_S-2222222222.md').write_bytes((
            '---\nid: S-2222222222\ntitle: Kraków deed\nsource_type: other\n---\n\n'
            '## Claims\n```yaml\n- id: C-3333333333\n'
            '  type: birth\n  persons: ["[[P-8888888888|Another Ghost]]"]\n'
            '  value: born sometime\n  status: suggested\n  confidence: low\n```\n'
        ).encode('cp1252'))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_scan_does_not_crash_on_either_bad_file(self) -> None:
        # Pre-fix, either read_record(path) call (people/ or sources/) raises
        # UnicodeDecodeError with no try/except at all, and the WHOLE stubs
        # scan crashes before it can report anything.
        unresolved = stubs._collect_unresolved_persons(self.root)
        self.assertIsInstance(unresolved, dict)

    def test_readable_files_still_contribute_despite_the_bad_ones(self) -> None:
        # The good source's unresolved P-9999999999 reference must still be
        # found even though a sibling source and a sibling person record
        # could not be read this run - one bad file must not blind the scan
        # to every other file.
        unresolved = stubs._collect_unresolved_persons(self.root)
        self.assertIn('p-9999999999', unresolved)
        # The known person (P-1111111111) is still recognized, so no stub
        # would be minted for someone who already has a record.
        self.assertNotIn('p-1111111111', unresolved)

    def test_aggregated_warning_names_both_skipped_files(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            stubs._collect_unresolved_persons(self.root)
        text = stderr.getvalue()
        self.assertNotIn('Traceback', text)
        self.assertIn('2 file(s)', text)
        self.assertIn('muller__anne_P-2222222222.md', text)
        self.assertIn('krakow_S-2222222222.md', text)
        self.assertIn('not saved as UTF-8', text)


if __name__ == '__main__':
    unittest.main()
