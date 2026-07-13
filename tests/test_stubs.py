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

import datetime
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

    def test_stub_content_wrapper_delegates_byte_identical(self) -> None:
        self.assertEqual(
            stubs._stub_content('P-aaaaaaaaaa', 'Jane Doe'),
            render_stub_content('P-aaaaaaaaaa', 'Jane Doe'),
        )


if __name__ == '__main__':
    unittest.main()
