"""
test_alias_mirror.py - a person's names are listed in their `aliases:`.

Obsidian offers a note while the owner types `[[` by searching file names and
each note's frontmatter `aliases:`. SPEC §9 and §18 say a person's `name:` and
public other names (`name_variants:`, `name_at_birth:`, `married_name:` and
`also_known_as:`) are mirrored there, after the ID, so typing `[[Tom` finds
Thomas Edward Hartley and typing a nickname or a maiden name finds him too. These tests pin that rule where it is written:

  - the shared rule itself (`_lib.person_alias_mirror`, `sync_person_aliases`);
  - every verb that writes a name: `fha person new`, `fha person rename`
    (with and without `--keep-variant`), `fha gedcom import`'s stubs, the
    survivor of `fha confirm merge`, and `fha lint --fix-ids`;
  - the backfill: lint W111 names a record whose mirror is behind, and
    `fha lint --fix-aliases` brings it up to date (dry run first);
  - privacy: a restricted name, in any of those fields, is never written into `aliases:`, is
    taken out if it is there, and a packet copy of a synced profile does not
    carry it;
  - the alias-clash check reports two people of one name once, mirrored or
    not.

Expected values come from SPEC §9/§18, not from the implementation's output.
"""

import shutil
import sys
import tempfile
import unittest

import yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import gedcom_import
import lint
import packet
import person
from _lib import (
    person_alias_mirror,
    read_record,
    render_stub_content,
    sync_person_aliases,
    yaml_alias_entry,
)

PID = 'p-de957bcda1'


def _person(aliases_line: str, *, name: str = 'Thomas Edward Hartley',
            extra: str = '') -> str:
    return (
        '---\n'
        f'id: {PID}\n'
        f'{aliases_line}'
        f'name: {name}\n'
        f'{extra}'
        'living: false\n'
        'tier: curated\n'
        '---\n\n'
        '## Biography\n'
    )


def _aliases(text: str) -> list:
    import yaml
    return yaml.safe_load(text.split('---\n')[1]).get('aliases')


class MirrorRuleTests(unittest.TestCase):
    """The rule: ID first, then name, then public variants, no repeats."""

    def test_name_then_public_variants_without_repeats(self):
        meta = {'name': 'Thomas Edward Hartley',
                'name_variants': ['T. E. Hartley', 'thomas edward hartley',
                                  {'value': 'Tom Hartley'}]}
        self.assertEqual(person_alias_mirror(meta),
                         ['Thomas Edward Hartley', 'T. E. Hartley', 'Tom Hartley'])

    def test_restricted_variant_and_unknown_name_are_left_out(self):
        meta = {'name': 'unknown',
                'name_variants': [{'value': 'Quenby Talbot', 'restricted': True},
                                  {'value': 'Wren', 'restricted': 'by-request'}]}
        self.assertEqual(person_alias_mirror(meta), [])

    def test_sync_adds_names_after_the_id_in_a_flow_list(self):
        text = _person(f'aliases: [{PID}]\n',
                       extra='name_variants: [T. E. Hartley]\n')
        new, added, removed = sync_person_aliases(text)
        self.assertEqual(_aliases(new), [PID, 'Thomas Edward Hartley', 'T. E. Hartley'])
        self.assertEqual(added, ['Thomas Edward Hartley', 'T. E. Hartley'])
        self.assertEqual(removed, [])
        # Everything but the aliases: line is byte-for-byte the same.
        self.assertEqual(new.replace(new.splitlines()[2], ''),
                         text.replace(text.splitlines()[2], ''))

    def test_sync_keeps_human_entries_and_their_comments(self):
        text = _person('aliases:\n  - grandpa-tom   # what Dad called him\n')
        new, _, _ = sync_person_aliases(text)
        self.assertIn('  - grandpa-tom   # what Dad called him\n', new)
        self.assertEqual(_aliases(new), [PID, 'grandpa-tom', 'Thomas Edward Hartley'])

    def test_sync_quotes_a_name_a_flow_list_would_split(self):
        text = _person(f'aliases: [{PID}]\n', name="'Hartley, Thomas'")
        new, _, _ = sync_person_aliases(text)
        self.assertEqual(_aliases(new), [PID, 'Hartley, Thomas'])

    def test_sync_keeps_crlf_line_endings(self):
        text = _person(f'aliases: [{PID}]\n').replace('\n', '\r\n')
        new, added, _ = sync_person_aliases(text)
        self.assertTrue(added)
        self.assertNotIn('\n', new.replace('\r\n', ''))

    def test_sync_takes_a_restricted_variant_out(self):
        text = _person(f'aliases: [{PID}, Quenby Talbot]\n',
                       extra='name_variants:\n  - {value: Quenby Talbot, restricted: true}\n')
        new, _, removed = sync_person_aliases(text)
        self.assertEqual(removed, ['Quenby Talbot'])
        self.assertEqual(_aliases(new), [PID, 'Thomas Edward Hartley'])

    def test_sync_never_removes_an_old_name(self):
        # An old name stays, so a [[Thomas Hartly]] link written under it
        # keeps resolving; only a restricted variant is ever taken out.
        text = _person(f'aliases: [{PID}, Thomas Hartly, grandpa-tom]\n')
        new, _, removed = sync_person_aliases(text)
        self.assertEqual(removed, [])
        self.assertEqual(_aliases(new),
                         [PID, 'Thomas Hartly', 'grandpa-tom', 'Thomas Edward Hartley'])

    def test_bracket_in_a_trailing_comment_is_not_the_lists_end(self):
        line = f'aliases: [{PID}, Tom]  # Tom [his old vault note]\n'
        new, added, _ = sync_person_aliases(_person(line))
        self.assertEqual(added, ['Thomas Edward Hartley'])
        self.assertEqual(_aliases(new), [PID, 'Tom', 'Thomas Edward Hartley'])
        self.assertIn('  # Tom [his old vault note]\n', new)
        self.assertEqual(sync_person_aliases(new), (new, [], []))   # settled

    def test_removing_a_restricted_alias_keeps_a_bracketed_comment(self):
        line = f'aliases: [{PID}, Thomas Edward Hartley, Wren]  # Wren was [a stage name]\n'
        text = _person(line, extra='name_variants:\n  - {value: Wren, restricted: true}\n')
        new, _, removed = sync_person_aliases(text)
        self.assertEqual(removed, ['Wren'])
        self.assertEqual(_aliases(new), [PID, 'Thomas Edward Hartley'])
        self.assertIn('  # Wren was [a stage name]\n', new)

    def test_trailing_comma_in_a_flow_list_is_not_doubled(self):
        for line in (f'aliases: [{PID}, ]\n', f'aliases: [{PID},]\n',
                     f'aliases: [{PID}, Tom , ]  # note [x]\n'):
            with self.subTest(line=line):
                new, added, _ = sync_person_aliases(_person(line))
                self.assertIn('Thomas Edward Hartley', added)
                self.assertIn('Thomas Edward Hartley', _aliases(new))
                self.assertNotIn(',,', new)
                self.assertEqual(sync_person_aliases(new), (new, [], []))

    def test_an_edit_that_would_not_read_back_is_not_made(self):
        # Shapes the surgery cannot read safely are left exactly as written,
        # never turned into a record YAML cannot parse.
        for line in (f'aliases: [{PID}, ]# x]\n', f'aliases: [{PID}, \\,]\n'):
            with self.subTest(line=line):
                text = _person(line)
                new, _, _ = sync_person_aliases(text)
                yaml.safe_load(new.split('---\n')[1])   # must still parse
                if new != text:
                    self.assertIn('Thomas Edward Hartley', _aliases(new))

    def test_names_yaml_would_read_as_another_type_are_quoted(self):
        for name in ('No', 'yes', 'On', 'null', '123', '1923-01-02', '0x1F', '1e3'):
            with self.subTest(name=name):
                entry = yaml_alias_entry(name)
                self.assertEqual(yaml.safe_load(f'[{entry}]'), [name])
                text = _person(f'aliases: [{PID}]\n', name=yaml.safe_dump(name).split('\n')[0])
                new, _, _ = sync_person_aliases(text)
                self.assertEqual(_aliases(new), [PID, name])
                self.assertEqual(sync_person_aliases(new), (new, [], []))

    def test_sync_leaves_alone_records_outside_the_alias_layer(self):
        no_key = _person('')
        tombstone = _person(f'aliases: [{PID}]\n',
                            extra='status: merged\nmerged_into: p-0a1b2c3d4e\n')
        for text in (no_key, tombstone):
            with self.subTest(text=text[:60]):
                self.assertEqual(sync_person_aliases(text), (text, [], []))


class NameWritingVerbTests(unittest.TestCase):
    """Every verb that writes a person's name leaves the mirror in step."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                            encoding='utf-8')
        (self.root / 'people' / 'stubs').mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_new_stub_lists_its_id_then_its_name(self):
        text = render_stub_content('p-aaaaaaaaaa', 'Ada Jane Hartley')
        self.assertIn('aliases: [p-aaaaaaaaaa, Ada Jane Hartley]\n', text)
        self.assertIn('aliases: [p-aaaaaaaaaa]\n', render_stub_content('p-aaaaaaaaaa', None))

    def test_person_new_writes_the_mirror(self):
        res = person.run_new(self.root, 'Margaret A. Cole')
        self.assertEqual(res.exit_code, 0, res.messages)
        path = next((self.root / 'people' / 'stubs').glob('cole__margaret_a_p-*.md'))
        meta = read_record(path)['meta']
        self.assertEqual(meta['aliases'], [meta['id'], 'Margaret A. Cole'])

    def _rename_fixture(self):
        path = self.root / 'people' / 'stubs' / f'hartly__thomas_{PID}.md'
        path.write_text(_person(f'aliases: [{PID}, Thomas Hartly]\n', name='Thomas Hartly'),
                        encoding='utf-8')
        return path

    def _renamed(self):
        return read_record(next(self.root.rglob(f'*_{PID}.md')))['meta']

    def test_rename_adds_the_new_name_and_keeps_the_old(self):
        # The old name stays in aliases: so [[Thomas Hartly]] links keep
        # resolving; without --keep-variant it leaves name_variants: alone.
        self._rename_fixture()
        res = person.run_rename(self.root, PID, 'Thomas Hartley')
        self.assertEqual(res.exit_code, 0, res.messages)
        meta = self._renamed()
        self.assertEqual(meta['aliases'], [PID, 'Thomas Hartly', 'Thomas Hartley'])
        self.assertNotIn('name_variants', meta)
        res = lint.run_lint(self.root, {})
        self.assertEqual([m for m in res.messages if m.code == 'W111'], [])

    def test_rename_keep_variant_keeps_the_old_name(self):
        self._rename_fixture()
        res = person.run_rename(self.root, PID, 'Thomas Hartley', keep_variant=True)
        self.assertEqual(res.exit_code, 0, res.messages)
        meta = self._renamed()
        self.assertEqual(meta['name_variants'], ['Thomas Hartly'])
        self.assertEqual(meta['aliases'], [PID, 'Thomas Hartly', 'Thomas Hartley'])

    def test_rename_dry_run_names_the_alias_change_and_writes_nothing(self):
        path = self._rename_fixture()
        before = path.read_text(encoding='utf-8')
        res = person.run_rename(self.root, PID, 'Thomas Hartley', dry_run=True)
        self.assertEqual(path.read_text(encoding='utf-8'), before)
        shown = ' '.join(m.text for m in res.messages)
        self.assertIn("'Thomas Hartley' to aliases:", shown)
        self.assertNotIn('out of aliases:', shown)
        self.assertIn("'Thomas Hartly' stays in aliases:", shown)

    def test_rename_says_the_old_name_is_kept_and_how_to_drop_it(self):
        # Keeping the old name can clash with a real person of that name added
        # later, so the rename says so once, with the hand fix.
        self._rename_fixture()
        res = person.run_rename(self.root, PID, 'Thomas Hartley')
        shown = ' '.join(m.text for m in res.messages)
        self.assertIn("'Thomas Hartly' stays in aliases:", shown)
        self.assertIn('belongs to someone else', shown)

    def test_rename_works_on_a_flow_list_with_a_trailing_comma(self):
        path = self.root / 'people' / 'stubs' / f'hartly__thomas_{PID}.md'
        path.write_text(_person(f'aliases: [{PID}, Thomas Hartly, ]\n', name='Thomas Hartly'),
                        encoding='utf-8')
        res = person.run_rename(self.root, PID, 'Thomas Hartley')
        self.assertEqual(res.exit_code, 0, res.messages)
        self.assertEqual(self._renamed()['aliases'], [PID, 'Thomas Hartly', 'Thomas Hartley'])

    def test_gedcom_import_stub_lists_name_and_variants(self):
        stub = gedcom_import._PersonStub.__new__(gedcom_import._PersonStub)
        for field, value in {'pid': 'p-aaaaaaaaaa', 'name': 'Ada Jane Hartley',
                             'variants': ['Ada J. Hartley'], 'recorded_surname': None,
                             'sex': None, 'living': 'unknown', 'birth': None,
                             'death': None}.items():
            setattr(stub, field, value)
        text = gedcom_import._person_stub_text(stub, '2026-11-20')
        self.assertIn('aliases: [p-aaaaaaaaaa, Ada Jane Hartley, Ada J. Hartley]\n', text)

    def test_fix_ids_lists_the_name_before_the_old_file_name(self):
        (self.root / 'people' / 'Grandma Ada.md').write_text(
            '---\nname: Ada Jane Hartley\nliving: false\n---\n\n# Ada\n', encoding='utf-8')
        lint.run_lint(self.root, {}, fix_ids=True)
        path = next((self.root / 'people').glob('hartley__ada_jane_p-*.md'))
        meta = read_record(path)['meta']
        self.assertEqual(meta['aliases'],
                         [meta['id'], 'Ada Jane Hartley', 'grandma-ada', 'Grandma Ada'])


class BackfillTests(unittest.TestCase):
    """W111 names a person whose mirror is behind; --fix-aliases fixes it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                            encoding='utf-8')
        self.people = self.root / 'people' / 'stubs'
        self.people.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, stem, text):
        path = self.people / f'{stem}.md'
        path.write_text(text, encoding='utf-8')
        return path

    def _w111(self, **kw):
        res = lint.run_lint(self.root, {}, **kw)
        return res, [m for m in res.messages if m.code == 'W111']

    def test_behind_mirror_is_reported_then_fixed(self):
        path = self._write(f'hartley__thomas_edward_{PID}',
                           _person(f'aliases: [{PID}]\n',
                                   extra='name_variants: [T. E. Hartley]\n'))
        before = path.read_text(encoding='utf-8')
        _, found = self._w111()
        self.assertEqual(len(found), 1)
        self.assertIn('Thomas Edward Hartley, T. E. Hartley', found[0].text)
        self.assertIn('fha lint --fix-aliases', found[0].text)

        res, _ = self._w111(fix_aliases=True, dry_run=True)
        self.assertEqual(path.read_text(encoding='utf-8'), before)
        self.assertTrue(any('--fix-aliases dry-run: would add' in p
                            for p in res.data['progress']), res.data['progress'])

        self._w111(fix_aliases=True)
        self.assertEqual(read_record(path)['meta']['aliases'],
                         [PID, 'Thomas Edward Hartley', 'T. E. Hartley'])
        self.assertEqual(self._w111()[1], [])

    def test_restricted_variant_in_aliases_is_reported_without_naming_it(self):
        path = self._write(f'hartley__thomas_edward_{PID}', _person(
            f'aliases: [{PID}, Thomas Edward Hartley, Quenby Talbot]\n',
            extra='name_variants:\n  - {value: Quenby Talbot, restricted: true}\n'))
        _, found = self._w111()
        self.assertEqual(len(found), 1)
        self.assertIn('restricted name', found[0].text)
        self.assertNotIn('Quenby', found[0].text)
        self._w111(fix_aliases=True)
        self.assertEqual(read_record(path)['meta']['aliases'], [PID, 'Thomas Edward Hartley'])

    def test_records_outside_the_alias_layer_are_not_reported(self):
        self._write(f'hartley__thomas_edward_{PID}', _person(''))
        self._write('MERGED-INTO-p-0a1b2c3d4e__hartley__tom_p-1b2c3d4e5f', (
            '---\nid: p-1b2c3d4e5f\naliases: [p-1b2c3d4e5f]\nname: Tom Hartley\n'
            'status: merged\nmerged_into: p-0a1b2c3d4e\nliving: false\n---\n'))
        self.assertEqual(self._w111()[1], [])

    def test_tombstone_is_checked_for_its_own_id_only(self):
        path = self._write('MERGED-INTO-p-0a1b2c3d4e__hartley__tom_p-1b2c3d4e5f', (
            '---\nid: p-1b2c3d4e5f\naliases: [grandpa-tom]\nname: Tom Hartley\n'
            'status: merged\nmerged_into: p-0a1b2c3d4e\nliving: false\n---\n'))
        _, found = self._w111()
        self.assertEqual(len(found), 1)
        self.assertIn("own ID p-1b2c3d4e5f", found[0].text)
        self.assertNotIn('Tom Hartley', found[0].text)
        self._w111(fix_aliases=True)
        self.assertEqual(read_record(path)['meta']['aliases'], ['p-1b2c3d4e5f', 'grandpa-tom'])

    def test_single_value_aliases_missing_the_id_is_named_for_a_hand_edit(self):
        path = self._write(f'hartley__thomas_edward_{PID}',
                           _person('aliases: grandpa-tom\n'))
        before = path.read_text(encoding='utf-8')
        _, found = self._w111()
        self.assertEqual(len(found), 1)
        res, _ = self._w111(fix_aliases=True)
        self.assertEqual(path.read_text(encoding='utf-8'), before)
        self.assertTrue(any('by hand' in p for p in res.data['progress']), res.data['progress'])

    def test_source_missing_its_own_id_gets_it_first(self):
        src = self.root / 'sources' / 'letters'
        src.mkdir(parents=True)
        path = src / 'grandpas-letter_s-b237895f31.md'
        path.write_text('---\nid: s-b237895f31\naliases: [grandpas-letter]\n'
                        'title: Letter\nsource_type: letter\n---\n\n## Claims\n',
                        encoding='utf-8')
        _, found = self._w111()
        self.assertEqual(len(found), 1)
        self._w111(fix_aliases=True)
        self.assertEqual(read_record(path)['meta']['aliases'],
                         ['s-b237895f31', 'grandpas-letter'])

    def test_two_people_of_one_name_clash_once_mirrored_or_not(self):
        for pid in ('p-1111111111', 'p-2222222222'):
            self._write(f'smith__john_{pid}', (
                f'---\nid: {pid}\naliases: [{pid}]\nname: John Smith\n'
                'living: false\ntier: stub\n---\n'))

        def w112():
            res = lint.run_lint(self.root, {})
            return [m for m in res.messages if m.code in ('W112', 'W113')]

        before = w112()
        self.assertEqual(len(before), 1)
        lint.run_lint(self.root, {}, fix_aliases=True)
        self.assertEqual([m.text for m in w112()], [m.text for m in before])


class ExportPrivacyTests(unittest.TestCase):
    """A synced profile never carries a restricted name into a packet copy.

    The site, GEDCOM and WikiTree exporters read names from the index (which
    reads `name_variants:` with its restricted marker), never from a
    profile's `aliases:`; the packet is the one exporter that copies the
    profile file itself, so it is the one checked here."""

    def test_packet_copy_of_a_synced_profile_has_no_restricted_name(self):
        text = _person(f'aliases: [{PID}]\n', extra=(
            'name_variants:\n  - T. E. Hartley\n'
            '  - {value: Quenby Talbot, restricted: true}\n'))
        synced, _, _ = sync_person_aliases(text)
        self.assertNotIn('Quenby', synced.split('name_variants:')[0])
        redacted = packet._redact_profile_text(
            synced, include_restricted=False, include_dna=False)
        self.assertIsNotNone(redacted)
        self.assertNotIn('Quenby', redacted[0])
        self.assertEqual(_aliases(redacted[0]), [PID, 'Thomas Edward Hartley', 'T. E. Hartley'])



class OtherNameFieldTests(unittest.TestCase):
    """A nickname, a birth name and a married name are mirrored too (SPEC §9).

    Each field may be one string, a list, or hold `{value:, restricted:}`
    entries. A restricted value never reaches `aliases:`, and no output line
    names one."""

    PRIVATE = 'Quenby Talbot'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots:\n  documents: documents\n',
                                            encoding='utf-8')
        self.people = self.root / 'people' / 'stubs'
        self.people.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_mirror_lists_every_name_field_in_record_order(self):
        meta = {'name': 'Margaret A. Cole',
                'name_variants': ['Maggie Cole'],
                'name_at_birth': 'Margaret Ann Cole',
                'married_name': {'value': 'Margaret Hartley'},
                'also_known_as': ['Peggy', 'maggie cole']}
        self.assertEqual(person_alias_mirror(meta),
                         ['Margaret A. Cole', 'Maggie Cole', 'Margaret Ann Cole',
                          'Margaret Hartley', 'Peggy'])

    def test_one_string_also_known_as_is_mirrored(self):
        self.assertEqual(person_alias_mirror({'name': 'Edith Hartley', 'also_known_as': 'Edie'}),
                         ['Edith Hartley', 'Edie'])

    def test_restricted_values_in_every_field_are_left_out(self):
        for field in ('also_known_as', 'name_at_birth', 'married_name'):
            for value in ({'value': self.PRIVATE, 'restricted': True},
                          [{'value': self.PRIVATE, 'restricted': 'by-request'}]):
                with self.subTest(field=field, value=value):
                    meta = {'name': 'Edith Hartley', field: value}
                    self.assertEqual(person_alias_mirror(meta), ['Edith Hartley'])

    def test_a_name_restricted_in_one_field_is_not_mirrored_from_another(self):
        meta = {'name': 'Edith Hartley',
                'also_known_as': [self.PRIVATE],
                'name_variants': [{'value': self.PRIVATE, 'restricted': True}]}
        self.assertEqual(person_alias_mirror(meta), ['Edith Hartley'])

    def test_sync_adds_the_nickname_and_takes_a_restricted_birth_name_out(self):
        text = _person(f'aliases: [{PID}, Thomas Edward Hartley, {self.PRIVATE}]\n', extra=(
            f'name_at_birth: {{value: {self.PRIVATE}, restricted: true}}\n'
            'also_known_as: [Ned]\n'))
        new, added, removed = sync_person_aliases(text)
        self.assertEqual(added, ['Ned'])
        self.assertEqual(removed, [self.PRIVATE])
        self.assertEqual(_aliases(new), [PID, 'Thomas Edward Hartley', 'Ned'])

    def _w111(self, **kw):
        res = lint.run_lint(self.root, {}, **kw)
        return res, [m for m in res.messages if m.code == 'W111']

    def test_w111_names_a_missing_nickname_and_fix_aliases_adds_it(self):
        path = self.people / f'hartley__thomas_edward_{PID}.md'
        path.write_text(_person(f'aliases: [{PID}, Thomas Edward Hartley]\n', extra=(
            'married_name: Tom Easton\nalso_known_as: [Ned]\n')), encoding='utf-8')
        _, found = self._w111()
        self.assertEqual(len(found), 1)
        self.assertIn('Tom Easton, Ned', found[0].text)
        self._w111(fix_aliases=True)
        self.assertEqual(read_record(path)['meta']['aliases'],
                         [PID, 'Thomas Edward Hartley', 'Tom Easton', 'Ned'])
        self.assertEqual(self._w111()[1], [])

    def test_restricted_married_name_in_aliases_is_never_printed(self):
        path = self.people / f'hartley__thomas_edward_{PID}.md'
        path.write_text(_person(f'aliases: [{PID}, Thomas Edward Hartley, {self.PRIVATE}]\n',
                                extra=(f'married_name:\n  value: {self.PRIVATE}\n'
                                       '  restricted: true\nalso_known_as: Ned\n')),
                        encoding='utf-8')
        _, found = self._w111()
        self.assertEqual(len(found), 1)
        self.assertIn('restricted name', found[0].text)
        res, _ = self._w111(fix_aliases=True, dry_run=True)
        res2, _ = self._w111(fix_aliases=True)
        shown = ' '.join([m.text for m in found] + res.data['progress'] + res2.data['progress'])
        self.assertNotIn('Quenby', shown)
        self.assertEqual(read_record(path)['meta']['aliases'],
                         [PID, 'Thomas Edward Hartley', 'Ned'])

    def test_rename_keeps_the_old_name_and_never_mirrors_a_private_one(self):
        path = self.people / f'hartly__thomas_{PID}.md'
        path.write_text(_person(f'aliases: [{PID}, Thomas Hartly]\n', name='Thomas Hartly', extra=(
            f'also_known_as:\n  - Ned\n  - {{value: {self.PRIVATE}, restricted: true}}\n')),
            encoding='utf-8')
        res = person.run_rename(self.root, PID, 'Thomas Hartley')
        self.assertEqual(res.exit_code, 0, res.messages)
        self.assertNotIn('Quenby', ' '.join(m.text for m in res.messages))
        meta = read_record(next(self.root.rglob(f'*_{PID}.md')))['meta']
        self.assertEqual(meta['aliases'], [PID, 'Thomas Hartly', 'Thomas Hartley', 'Ned'])


if __name__ == '__main__':
    unittest.main()
