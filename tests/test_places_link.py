"""
test_places_link.py - linking claims to registered places in bulk.

A genealogist who registered "Fairview" after typing "Fairview City, Breton,
Kansas" on a dozen census claims should not have to link them one by one, and
a backlog of recurring place names should become places in one reviewed step.
Covered here:

  - `_lib.add_place_to_claim`: adds exactly one `place:` line, and every other
    byte of the record survives (CRLF, comments, quoting, block scalars,
    unusual key columns, characters Python would read as line breaks);
  - `fha places link`: dry run by default, `--apply` writes, nothing is ever
    linked to a place text that fits two places, a failed write puts every
    record back;
  - `fha places candidates --register`: one list, one yes; GeoNames
    coordinates from the saved data only; two spellings of one town become
    one place; a cluster that fits a registered place joins it; a registry
    edited while the question was open is respected.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))

import _lib  # noqa: E402
import index  # noqa: E402
import places  # noqa: E402
from _lib import ClaimEditRefused, add_place_to_claim  # noqa: E402
from tests.test_places import _write_geonames  # noqa: E402

PID = 'l-7c1a9f4e22'


def _claims_of(text: str) -> list:
    lines = [ln[:-1] if ln.endswith('\r') else ln for ln in text.split('\n')]
    block = _lib._find_claims_block(lines)
    return yaml.safe_load('\n'.join(lines[block[0] + 1:block[1]]))


def _only_added_line(before: str, after: str) -> str:
    """The one line `after` has that `before` lacks; fails if anything else differs."""
    b, a = before.split('\n'), after.split('\n')
    assert len(a) == len(b) + 1, (b, a)
    for i in range(len(a)):
        if a[:i] + a[i + 1:] == b:
            return a[i]
    raise AssertionError('more than one line changed')


_RECORD = (
    '---\n'
    'id: s-aaaaaaaaa1\n'
    'title: "1880 census: Hartley household # 96"\n'
    'source_type: census\n'
    '---\n'
    '\n'
    '## Claims\n'
    '```yaml\n'
    '# a hand comment above the claims\n'
    '- id: c-aaaaaaaaa1\n'
    '  type: residence\n'
    '  persons: ["[[p-de957bcda1|Thomas Edward Hartley]]"]\n'
    "  value: 'Lot #4 of block 2'   # quoted because of the hash\n"
    '  status: accepted\n'
    '  confidence: medium\n'
    '  place_text: Fairview City, Breton, Kansas\n'
    '  notes: >\n'
    '    Enumerated on the 14th by the same hand.\n'
    '\n'
    '- id: c-aaaaaaaaa2\n'
    '  type: occupation\n'
    '  persons: [p-de957bcda1]\n'
    '  value: "bookkeeper"\n'
    '  status: suggested\n'
    '  confidence: medium\n'
    '  place_text: >-\n'
    '    Fairview City,\n'
    '    Breton, Kansas\n'
    '  anchor: line 4\n'
    '```\n'
    '\n'
    '## Notes\n'
    '- place_text: not a claim, just prose that looks like one\n'
    'Copied from the page' + chr(0x2028) + 'as written.\n'
)


class AddPlaceToClaimTests(unittest.TestCase):
    """Only the `place:` line is new; nothing else in the record moves."""

    def test_adds_one_line_after_place_text(self):
        after = add_place_to_claim(_RECORD, 'c-aaaaaaaaa1', PID)
        self.assertEqual(_only_added_line(_RECORD, after), f'  place: {PID}')
        lines = after.split('\n')
        self.assertEqual(lines[lines.index(f'  place: {PID}') - 1],
                         '  place_text: Fairview City, Breton, Kansas')
        claims = _claims_of(after)
        self.assertEqual(claims[0]['place'], PID)
        self.assertNotIn('place', claims[1])

    def test_multiline_place_text_keeps_its_continuation_lines(self):
        after = add_place_to_claim(_RECORD, 'c-aaaaaaaaa2', PID)
        lines = after.split('\n')
        at = lines.index(f'  place: {PID}')
        self.assertEqual(lines[at - 1], '    Breton, Kansas')
        self.assertEqual(lines[at + 1], '  anchor: line 4')
        self.assertEqual(_claims_of(after)[1]['place_text'], 'Fairview City, Breton, Kansas')

    def test_crlf_record_stays_crlf_byte_for_byte(self):
        crlf = _RECORD.replace('\n', '\r\n')
        after = add_place_to_claim(crlf, 'c-aaaaaaaaa1', PID)
        self.assertEqual(_only_added_line(crlf, after), f'  place: {PID}\r')
        self.assertNotIn('\n', after.replace('\r\n', ''))

    def test_line_separator_character_is_not_a_line_break(self):
        after = add_place_to_claim(_RECORD, 'c-aaaaaaaaa1', PID)
        self.assertIn('the page' + chr(0x2028) + 'as written', after)
        self.assertEqual(after.count(chr(0x2028)), 1)

    def test_record_without_final_newline(self):
        text = _RECORD.rstrip('\n')
        after = add_place_to_claim(text, 'c-aaaaaaaaa1', PID)
        self.assertFalse(after.endswith('\n'))
        _only_added_line(text, after)

    def test_place_text_as_the_first_key_on_the_dash_line(self):
        text = _RECORD.replace(
            '- id: c-aaaaaaaaa1\n',
            '- place_text: Fairview City, Breton, Kansas\n  id: c-aaaaaaaaa1\n').replace(
            '  confidence: medium\n  place_text: Fairview City, Breton, Kansas\n',
            '  confidence: medium\n', 1)
        after = add_place_to_claim(text, 'c-aaaaaaaaa1', PID)
        self.assertEqual(_only_added_line(text, after), f'  place: {PID}')
        self.assertEqual(_claims_of(after)[0]['place'], PID)

    def test_keys_at_column_four(self):
        text = ('## Claims\n```yaml\n-   id: c-aaaaaaaaa1\n    type: residence\n'
                '    place_text: Fairview\n    status: accepted\n```\n')
        after = add_place_to_claim(text, 'c-aaaaaaaaa1', PID)
        self.assertEqual(_only_added_line(text, after), f'    place: {PID}')

    def test_empty_place_line_is_filled_keeping_its_comment(self):
        text = _RECORD.replace('  place_text: Fairview City, Breton, Kansas\n',
                               '  place:    # fill in later\n'
                               '  place_text: Fairview City, Breton, Kansas\n', 1)
        after = add_place_to_claim(text, 'c-aaaaaaaaa1', PID)
        self.assertIn(f'  place: {PID}    # fill in later\n', after)
        self.assertEqual(len(after.split('\n')), len(text.split('\n')))
        self.assertEqual(_claims_of(after)[0]['place'], PID)

    def test_claim_that_names_a_place_is_refused(self):
        text = _RECORD.replace('  confidence: medium\n  place_text: Fairview City',
                               '  confidence: medium\n  place: l-9e3c1b6a44\n'
                               '  place_text: Fairview City', 1)
        with self.assertRaises(ClaimEditRefused):
            add_place_to_claim(text, 'c-aaaaaaaaa1', PID)

    def test_claim_without_place_text_is_refused(self):
        text = _RECORD.replace('  place_text: Fairview City, Breton, Kansas\n', '', 1)
        with self.assertRaises(ClaimEditRefused):
            add_place_to_claim(text, 'c-aaaaaaaaa1', PID)

    def test_duplicate_claim_id_is_refused(self):
        text = _RECORD.replace('c-aaaaaaaaa2', 'c-aaaaaaaaa1')
        with self.assertRaises(ClaimEditRefused):
            add_place_to_claim(text, 'c-aaaaaaaaa1', PID)

    def test_unknown_claim_is_refused(self):
        with self.assertRaises(ClaimEditRefused):
            add_place_to_claim(_RECORD, 'c-bbbbbbbbb1', PID)

    def test_flow_style_neighbour_is_left_as_it_is(self):
        text = _RECORD.replace('\n- id: c-aaaaaaaaa2\n', '\n- {id: c-aaaaaaaa22, type: note,'
                               ' persons: [p-de957bcda1], value: x, status: suggested}\n'
                               '- id: c-aaaaaaaaa2\n')
        after = add_place_to_claim(text, 'c-aaaaaaaaa2', PID)
        self.assertEqual(_only_added_line(text, after), f'  place: {PID}')
        self.assertEqual(_claims_of(after)[2]['place'], PID)


# ── Archive fixtures for the engines ──────────────────────────────────────────

def _source(sid: str, claims: list[tuple[str, str]]) -> str:
    body = ''.join(
        f'- id: {cid}\n  type: residence\n  persons: [p-de957bcda1]\n  value: lived there\n'
        f'  status: accepted\n  confidence: medium\n  place_text: {yaml_text}\n\n'
        for cid, yaml_text in claims)
    return (f'---\nid: {sid}\ntitle: Record {sid}\nsource_type: census\n---\n\n'
            f'## Claims\n```yaml\n{body}```\n')


class _ArchiveCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots: {}\n', encoding='utf-8')
        (self.root / 'places').mkdir()
        (self.root / 'sources').mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def registry(self, text: str):
        (self.root / 'places' / 'places.yaml').write_text(text, encoding='utf-8')

    def source(self, name: str, sid: str, claims, *, crlf: bool = False) -> Path:
        path = self.root / 'sources' / f'{name}_{sid}.md'
        text = _source(sid, claims)
        if crlf:
            text = text.replace('\n', '\r\n')
        path.write_bytes(text.encode('utf-8'))
        return path

    def build(self):
        index.build_index(self.root, {})

    def snapshot(self) -> dict:
        return {p: p.read_bytes() for p in self.root.rglob('*')
                if p.is_file() and '.cache' not in p.parts}


_FAIRVIEW = (
    '# the registry\n'
    f'- id: {PID}\n'
    '  name: Fairview\n'
    '  hierarchy: Fairview, Breton County, Kansas, USA\n'
    '  alt_names: [Fairview City]\n'
)


class PlacesLinkTests(_ArchiveCase):

    def setUp(self):
        super().setUp()
        self.registry(_FAIRVIEW + '- id: l-9e3c1b6a44\n  name: Springfield\n'
                      '  hierarchy: Springfield, Sangamon County, Illinois, USA\n'
                      '- id: l-ab4d2c7b55\n  name: Springfield\n'
                      '  hierarchy: Springfield, Greene County, Missouri, USA\n')
        self.census = self.source('census', 's-aaaaaaaaa1', [
            ('c-aaaaaaaaa1', 'Fairview City, Breton, Kansas'),
            ('c-aaaaaaaaa2', 'Fairview, Kansas'),
            ('c-aaaaaaaaa3', 'Springfield'),
            ('c-aaaaaaaaa4', 'City, Fairview'),
        ], crlf=True)
        self.deed = self.source('deed', 's-aaaaaaaaa2', [
            ('c-aaaaaaaaa5', 'Fairview, Breton Co., Kansas, USA'),
            ('c-aaaaaaaaa6', 'Springfield, Illinois'),
            ('c-aaaaaaaaa7', 'Fairview, Ohio'),
        ])
        self.build()

    def test_dry_run_lists_and_writes_nothing(self):
        before = self.snapshot()
        result = places.run_link(self.root)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(result['linked'], 4)
        self.assertEqual(result['files'], 2)
        text = '\n'.join(result['messages'])
        self.assertIn('Would link 4 claims in 2 source records', text)
        self.assertIn('fha places link --apply', text)

    def test_apply_links_only_texts_that_fit_one_place(self):
        census_before = self.census.read_bytes()
        result = places.run_link(self.root, apply=True)
        self.assertEqual(result.exit_code, 0, result['messages'])
        census = _claims_of(self.census.read_bytes().decode('utf-8'))
        deed = _claims_of(self.deed.read_text(encoding='utf-8'))
        got = {c['id']: c.get('place') for c in census + deed}
        self.assertEqual(got, {
            'c-aaaaaaaaa1': PID, 'c-aaaaaaaaa2': PID, 'c-aaaaaaaaa5': PID,
            'c-aaaaaaaaa6': 'l-9e3c1b6a44',
            'c-aaaaaaaaa3': None,      # fits two Springfields: the human decides
            'c-aaaaaaaaa4': None,      # words in another order: close, never linked
            'c-aaaaaaaaa7': None,      # Ohio is not in Fairview's hierarchy
        })
        # place_text is never altered, and the CRLF record gained only two lines.
        self.assertEqual({c['id']: c['place_text'] for c in census}['c-aaaaaaaaa1'],
                         'Fairview City, Breton, Kansas')
        added = self.census.read_bytes().split(b'\r\n')
        self.assertEqual(len(added), len(census_before.split(b'\r\n')) + 2)
        self.assertEqual([ln for ln in added if ln not in census_before.split(b'\r\n')],
                         [f'  place: {PID}'.encode()] * 2)
        self.assertNotIn(b'\n', self.census.read_bytes().replace(b'\r\n', b''))

    def test_ambiguous_and_near_texts_name_the_command_that_settles_them(self):
        text = '\n'.join(places.run_link(self.root)['messages'])
        self.assertIn('fha confirm place c-aaaaaaaaa3 --into l-9e3c1b6a44', text)
        self.assertIn('fha confirm place c-aaaaaaaaa3 --into l-ab4d2c7b55', text)
        self.assertIn(f'fha confirm place c-aaaaaaaaa4 --into {PID}', text)

    def test_second_run_has_nothing_left_to_link(self):
        places.run_link(self.root, apply=True)
        self.build()
        before = self.snapshot()
        result = places.run_link(self.root, apply=True)
        self.assertEqual(result['linked'], 0)
        self.assertEqual(self.snapshot(), before)

    def test_failed_write_puts_every_record_back(self):
        before = self.snapshot()
        real = _lib.write_text_exact_atomic
        calls = []

        def flaky(path, text):
            calls.append(path)
            if len(calls) == 2:
                raise OSError('disk full')
            return real(path, text)

        with mock.patch.object(places, 'write_text_exact_atomic', flaky):
            result = places.run_link(self.root, apply=True)
        self.assertEqual(result.exit_code, 3)
        self.assertIn('Nothing was linked', '\n'.join(result['messages']))
        self.assertEqual(self.snapshot(), before)

    def test_stale_index_refuses(self):
        os.utime(self.deed, (os.path.getmtime(self.deed) + 100,) * 2)
        before = self.snapshot()
        result = places.run_link(self.root, apply=True)
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(self.snapshot(), before)

    def test_claim_with_its_own_unregistered_place_is_left_alone(self):
        text = self.deed.read_text(encoding='utf-8').replace(
            '  place_text: Fairview, Breton Co., Kansas, USA',
            '  place: l-0000000000\n  place_text: Fairview, Breton Co., Kansas, USA')
        self.deed.write_text(text, encoding='utf-8')
        self.build()
        places.run_link(self.root, apply=True)
        self.assertIn('  place: l-0000000000\n', self.deed.read_text(encoding='utf-8'))
        self.assertEqual(self.deed.read_text(encoding='utf-8').count('place:'), 2)

    def test_unreadable_registry_refuses(self):
        self.registry('- id: [unterminated\n')
        result = places.run_link(self.root, apply=True)
        self.assertEqual(result.exit_code, 3)


class RegisterCandidatesTests(_ArchiveCase):

    def setUp(self):
        super().setUp()
        self.registry('# no places yet\n')
        self.one = self.source('one', 's-aaaaaaaaa1', [
            ('c-aaaaaaaaa1', 'Fairview, Kansas'), ('c-aaaaaaaaa2', 'Fairview, Kansas'),
            ('c-aaaaaaaaa3', 'Fairview, Kansas'),
            ('c-aaaaaaaab1', 'Fairview, Breton County, Kansas'),
            ('c-aaaaaaaab2', 'Fairview, Breton County, Kansas'),
            ('c-aaaaaaaab3', 'Fairview, Breton County, Kansas'),
        ])
        self.two = self.source('two', 's-aaaaaaaaa2', [
            ('c-aaaaaaaac1', 'Kansas'), ('c-aaaaaaaac2', 'Kansas'), ('c-aaaaaaaac3', 'Kansas'),
            ('c-aaaaaaaad1', 'Nowhere Mills'), ('c-aaaaaaaad2', 'Nowhere Mills'),
            ('c-aaaaaaaad3', 'Nowhere Mills'),
            ('c-aaaaaaaae1', 'Topeka'),
        ], crlf=True)
        _write_geonames(self.root / '.cache' / 'geonames')
        self.build()

    def run_register(self, answers=(True,), **kw):
        prompts = []

        def confirm(prompt):
            prompts.append(prompt)
            return answers[len(prompts) - 1]
        return places.run_register_candidates(self.root, {}, confirm=confirm, **kw), prompts

    def registered(self) -> list[dict]:
        return yaml.safe_load((self.root / 'places' / 'places.yaml').read_text(encoding='utf-8'))

    def test_dry_run_shows_one_list_and_writes_nothing(self):
        before = self.snapshot()
        result, prompts = self.run_register(dry_run=True)
        self.assertEqual(prompts, [])
        self.assertEqual(self.snapshot(), before)
        text = '\n'.join(result['messages'])
        self.assertIn('Register 3 new places and link 12 claims', text)
        self.assertIn('coordinates 39.5631, -95.1216', text)
        self.assertIn('needs no coordinates', text)
        self.assertIn('[dry-run]', text)

    def test_decline_writes_nothing(self):
        before = self.snapshot()
        result, prompts = self.run_register(answers=(False,))
        self.assertEqual(len(prompts), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(result['registered'], 0)

    def test_yes_registers_and_links_in_one_go(self):
        result, prompts = self.run_register()
        self.assertEqual(len(prompts), 1, result['messages'])
        self.assertEqual(result['registered'], 3)
        self.assertEqual(result['linked'], 12)
        rows = {r['name']: r for r in self.registered()}
        # Two spellings of Fairview make one place, with GeoNames' pin.
        self.assertEqual(set(rows), {'Fairview, Breton County, Kansas', 'Kansas', 'Nowhere Mills'})
        fairview = rows['Fairview, Breton County, Kansas']
        self.assertEqual(fairview['coords'], [39.5631, -95.1216])
        self.assertEqual(fairview['hierarchy'], 'Fairview, Breton County, Kansas, United States')
        self.assertNotIn('coords', rows['Kansas'])
        self.assertNotIn('coords', rows['Nowhere Mills'])
        claims = {c['id']: c.get('place') for path in (self.one, self.two)
                  for c in _claims_of(path.read_bytes().decode('utf-8'))}
        self.assertEqual({claims[c] for c in ('c-aaaaaaaaa1', 'c-aaaaaaaab3')},
                         {fairview['id']})
        self.assertEqual(claims['c-aaaaaaaac1'], rows['Kansas']['id'])
        self.assertIsNone(claims['c-aaaaaaaae1'])   # one claim: below the threshold
        self.assertNotIn(b'\n', self.two.read_bytes().replace(b'\r\n', b''))
        self.assertIn('fha places geocode --all', '\n'.join(result['messages']))

    def test_cluster_that_fits_a_registered_place_joins_it(self):
        self.registry(_FAIRVIEW)
        self.build()
        result, _ = self.run_register()
        names = [r['name'] for r in self.registered()]
        self.assertEqual(names.count('Fairview'), 1)
        self.assertNotIn('Fairview, Kansas', names)
        self.assertNotIn('Fairview, Breton County, Kansas', names)
        claims = {c['id']: c.get('place') for c in _claims_of(self.one.read_text(encoding='utf-8'))}
        self.assertEqual(claims['c-aaaaaaaaa1'], PID)
        self.assertEqual(claims['c-aaaaaaaab1'], PID)

    def test_cluster_fitting_two_registered_places_goes_to_the_human(self):
        self.registry(_FAIRVIEW + '- id: l-9e3c1b6a44\n  name: Fairview\n'
                      '  hierarchy: Fairview, Monmouth County, Kansas, USA\n')
        self.build()
        result, _ = self.run_register()
        text = '\n'.join(result['messages'])
        names = [r['name'] for r in self.registered()]
        self.assertNotIn('Fairview, Kansas', names)
        self.assertIn(f'fha confirm place c-aaaaaaaaa1 c-aaaaaaaaa2 c-aaaaaaaaa3 --into {PID}',
                      text)

    def test_place_registered_while_the_question_was_open_is_left_out(self):
        def confirm(_prompt):
            path = self.root / 'places' / 'places.yaml'
            path.write_text(path.read_text(encoding='utf-8')
                            + '- id: l-9e3c1b6a44\n  name: Kansas\n', encoding='utf-8')
            return True
        result = places.run_register_candidates(self.root, {}, confirm=confirm)
        names = [r['name'] for r in self.registered()]
        self.assertEqual(names.count('Kansas'), 1)
        self.assertIn('was registered while the question was open',
                      '\n'.join(result['messages']))
        claims = {c['id']: c.get('place') for c in _claims_of(self.two.read_bytes().decode())}
        self.assertIsNone(claims['c-aaaaaaaac1'])

    def test_failed_write_puts_every_file_back(self):
        before = self.snapshot()
        real = _lib.write_text_exact_atomic
        calls = []

        def flaky(path, text):
            calls.append(path)
            if len(calls) == 3:
                raise OSError('disk full')
            return real(path, text)

        with mock.patch.object(places, 'write_text_exact_atomic', flaky):
            result, _ = self.run_register()
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(self.snapshot(), before)

    def test_without_saved_geonames_data_places_get_no_coordinates(self):
        import shutil
        shutil.rmtree(self.root / '.cache' / 'geonames')
        result, _ = self.run_register()
        self.assertTrue(all('coords' not in r for r in self.registered()))
        self.assertFalse((self.root / '.cache' / 'geonames').exists())   # nothing downloaded
        # Without GeoNames the two Fairview spellings still make one place:
        # "Fairview, Kansas" fits the hierarchy of "Fairview, Breton County, Kansas".
        self.assertEqual(result['registered'], 3)


class RegisterSpellingsTests(_ArchiveCase):
    """Without GeoNames data, two spellings of one town must still make one
    place, whichever spelling happens to be the bigger cluster."""

    def setUp(self):
        super().setUp()
        self.registry('# none yet\n')

    def run_register(self, sources):
        for name, sid, claims in sources:
            self.source(name, sid, claims)
        self.build()
        result = places.run_register_candidates(self.root, {}, confirm=lambda _p: True)
        rows = yaml.safe_load((self.root / 'places' / 'places.yaml').read_text(encoding='utf-8'))
        return result, [r['name'] for r in rows or []]

    def _two_spellings(self, short_n, long_n):
        claims = [(f'c-aaaaaaaa{i:02d}', 'Fairview, Kansas') for i in range(short_n)]
        claims += [(f'c-bbbbbbbb{i:02d}', 'Fairview, Breton County, Kansas') for i in range(long_n)]
        return [('one', 's-aaaaaaaaa1', claims)]

    def test_short_spelling_in_the_bigger_cluster(self):
        result, names = self.run_register(self._two_spellings(4, 3))
        self.assertEqual(names, ['Fairview, Breton County, Kansas'])
        self.assertEqual(result['linked'], 7)

    def test_list_names_the_other_spelling_it_takes_in(self):
        for name, sid, claims in self._two_spellings(4, 3):
            self.source(name, sid, claims)
        self.build()
        text = '\n'.join(places.run_register_candidates(self.root, {}, dry_run=True)['messages'])
        self.assertIn('New place "Fairview, Breton County, Kansas" (7 claims)', text)
        self.assertIn('    with "Fairview, Kansas" (4 claims)', text)
        self.assertNotIn('with "Fairview, Breton County, Kansas"', text)

    def test_long_spelling_in_the_bigger_cluster(self):
        result, names = self.run_register(self._two_spellings(3, 4))
        self.assertEqual(names, ['Fairview, Breton County, Kansas'])
        self.assertEqual(result['linked'], 7)

    def test_short_spelling_two_longer_ones_disagree_on_goes_to_the_human(self):
        claims = [(f'c-aaaaaaaa{i:02d}', 'Fairview, Kansas') for i in range(5)]
        claims += [(f'c-bbbbbbbb{i:02d}', 'Fairview, Breton County, Kansas') for i in range(3)]
        claims += [(f'c-dddddddd{i:02d}', 'Fairview, Ellis County, Kansas') for i in range(4)]
        result, names = self.run_register([('one', 's-aaaaaaaaa1', claims)])
        self.assertEqual(sorted(names), ['Fairview, Breton County, Kansas',
                                         'Fairview, Ellis County, Kansas'])
        self.assertEqual(result['linked'], 7)
        text = '\n'.join(result['messages'])
        self.assertIn('"Fairview, Kansas" (5 claims)', text)
        claims_now = {c['id']: c.get('place')
                      for c in _claims_of(self.root.joinpath(
                          'sources', 'one_s-aaaaaaaaa1.md').read_text(encoding='utf-8'))}
        self.assertIsNone(claims_now['c-aaaaaaaa00'])


class ManyClaimsInOneRecordTests(_ArchiveCase):
    """A GEDCOM import files every claim under one source record. Linking
    them must read that record's claims a fixed number of times, not once
    per claim (which took minutes at a few hundred claims)."""

    def test_record_is_parsed_a_fixed_number_of_times(self):
        n = 300
        path = self.source('big', 's-aaaaaaaaa1',
                           [(f'c-{i:010d}', 'Fairview, Kansas') for i in range(n)])
        links = {f'c-{i:010d}': PID for i in range(n)}
        calls = []
        real = _lib.safe_load_yaml

        def counting(text, *a, **kw):
            calls.append(1)
            return real(text, *a, **kw)

        with mock.patch.object(_lib, 'safe_load_yaml', counting), \
                mock.patch.object(places, 'safe_load_yaml', counting):
            plans, linked, skipped = places._plan_claim_links(
                self.root, links, {c: path for c in links}, lambda _c, _t: True)
        self.assertEqual((len(linked), skipped), (n, []))
        self.assertLessEqual(len(calls), 3)
        new = plans[path][1]
        self.assertEqual(new.count(f'  place: {PID}\n'), n)

    def test_one_odd_claim_does_not_make_the_record_slow(self):
        # A keep-chomped block scalar (|+) followed by a blank line changes
        # meaning when a line is added after it, so that claim is refused.
        # Finding it must not re-read the whole record once per claim.
        n = 400
        items = ['- id: c-keep000001\n  place_text: |+\n    Fairview, Kansas\n\n'
                 '  status: accepted\n']
        items += [f'- id: c-{i:010d}\n  place_text: Fairview, Kansas\n  status: accepted\n'
                  for i in range(n)]
        text = '## Claims\n```yaml\n' + ''.join(items) + '```\n'
        links = {'c-keep000001': PID, **{f'c-{i:010d}': PID for i in range(n)}}
        parsed = []
        real = _lib.safe_load_yaml

        def counting(t, *a, **kw):
            parsed.append(len(t))
            return real(t, *a, **kw)

        with mock.patch.object(_lib, 'safe_load_yaml', counting):
            new, linked, refused = _lib.add_places_to_claims(text, links)
        self.assertEqual(len(linked), n)
        self.assertEqual(list(refused), ['c-keep000001'])
        self.assertEqual(new.count(f'  place: {PID}\n'), n)
        # Every byte read is a small multiple of the record, not n times it.
        self.assertLess(sum(parsed), 10 * len(text))

    def test_anchors_and_merge_keys_hold_back_only_their_own_claims(self):
        # YAML anchors (&name), aliases (*name) and merge keys (<<:) tie one
        # claim's reading to another's, so a line added to one can change the
        # other. Those claims are left alone; the plain ones still link.
        text = ('## Claims\n```yaml\n'
                '- id: c-keep000001\n  place_text: |+\n    Fairview\n\n  status: accepted\n'
                '- &base\n  id: c-anch000001\n  status: accepted\n  place_text: Fairview\n'
                '- <<: *base\n  id: c-merg000001\n  place_text: Fairview, Kansas\n'
                '- id: c-defx000001\n  confidence: &x accepted\n  place_text: Fairview\n'
                '- id: c-alia000001\n  status: *x\n  place_text: Fairview\n'
                '- id: c-plain00001\n  place_text: Fairview\n'
                '- id: c-plain00002\n  place_text: Fairview\n'
                '```\n')
        ids = ['c-keep000001', 'c-anch000001', 'c-merg000001', 'c-defx000001',
               'c-alia000001', 'c-plain00001', 'c-plain00002']
        new, linked, refused = _lib.add_places_to_claims(text, {c: PID for c in ids})
        self.assertEqual(sorted(linked), ['c-plain00001', 'c-plain00002'])
        self.assertEqual(sorted(refused), sorted(ids[:5]))
        lines = new.split('\n')
        self.assertEqual(lines.count(f'  place: {PID}'), 2)
        self.assertEqual('\n'.join(ln for ln in lines if ln != f'  place: {PID}'), text)
        self.assertEqual([c.get('place') for c in _claims_of(new)][-2:], [PID, PID])

    def test_two_thousand_claims_link_in_one_pass(self):
        n = 2000
        text = _source('s-aaaaaaaaa1', [(f'c-{i:010d}', 'Fairview, Kansas') for i in range(n)])
        new, linked, refused = _lib.add_places_to_claims(
            text, {f'c-{i:010d}': PID for i in range(n)})
        self.assertEqual((len(linked), refused), (n, {}))
        self.assertEqual(len(new.split('\n')), len(text.split('\n')) + n)
        self.assertTrue(all(c['place'] == PID for c in _claims_of(new)))


class FailedRestoreTests(_ArchiveCase):
    """When a write fails and putting a file back fails too, the message must
    say which file kept the change; "every record is as it was" would be false."""

    def setUp(self):
        super().setUp()
        self.registry(_FAIRVIEW)
        self.a = self.source('a', 's-aaaaaaaaa1', [('c-aaaaaaaaa1', 'Fairview, Kansas')])
        self.b = self.source('b', 's-aaaaaaaaa2', [('c-aaaaaaaaa2', 'Fairview, Kansas')])
        self.build()

    def test_link_names_the_record_it_could_not_put_back(self):
        real = _lib.write_text_exact_atomic
        calls = []

        def locked(path, text):
            calls.append(path)
            if len(calls) == 1:
                return real(path, text)
            raise PermissionError(13, 'in use by another program', str(path))

        with mock.patch.object(places, 'write_text_exact_atomic', locked):
            result = places.run_link(self.root, apply=True)
        text = '\n'.join(result['messages'])
        self.assertEqual(result.exit_code, 3)
        self.assertNotIn('every record is as it was', text)
        self.assertIn('a_s-aaaaaaaaa1.md', text)
        self.assertIn(f'place: {PID}', self.a.read_text(encoding='utf-8'))

    def test_register_names_places_yaml_when_it_cannot_be_put_back(self):
        self.registry('# none yet\n')
        self.source('c', 's-aaaaaaaaa3', [(f'c-cccccccc{i:02d}', 'Nowhere Mills')
                                          for i in range(3)])
        self.build()
        real = _lib.write_text_exact_atomic
        calls = []

        def locked(path, text):
            calls.append(path)
            if len(calls) == 1:
                return real(path, text)
            raise PermissionError(13, 'in use by another program', str(path))

        with mock.patch.object(places, 'write_text_exact_atomic', locked):
            result = places.run_register_candidates(self.root, {}, confirm=lambda _p: True)
        text = '\n'.join(result['messages'])
        self.assertEqual(result.exit_code, 3)
        self.assertNotIn('every file is as it was', text)
        self.assertIn('places.yaml', text)


class CrlfRegistryTests(_ArchiveCase):

    def test_new_places_take_the_registry_line_endings(self):
        (self.root / 'places' / 'places.yaml').write_bytes(
            b'# my places\r\n- id: l-9e3c1b6a44\r\n  name: Topeka\r\n')
        self.source('one', 's-aaaaaaaaa1', [(f'c-aaaaaaaa{i:02d}', 'Nowhere Mills')
                                            for i in range(3)])
        self.build()
        places.run_register_candidates(self.root, {}, confirm=lambda _p: True)
        data = (self.root / 'places' / 'places.yaml').read_bytes()
        self.assertIn(b'name: Nowhere Mills\r\n', data)
        self.assertNotIn(b'\n', data.replace(b'\r\n', b''))


class LinkCliTests(_ArchiveCase):

    def test_dry_run_flag_without_register_is_refused(self):
        self.registry(_FAIRVIEW)
        self.build()
        import argparse
        args = argparse.Namespace(root=str(self.root), threshold=3, include_suggested=False,
                                  register=False, dry_run=True)
        self.assertEqual(places._cmd_places_candidates(args), 3)


if __name__ == '__main__':
    unittest.main()
