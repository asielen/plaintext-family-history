"""
test_name_privacy_exports.py: a restricted birth name, married name or
nickname stays home, like a restricted name variant (SPEC §9, §19).

SPEC §9 lets any of a person's name fields hold a `{value:, restricted:}`
entry, and promises the name is redacted on export. The packet copies the
profile file itself, and WikiTree publishes its prose, so both read every
name field through the one shared reader (`_lib.person_restricted_names`,
`_lib.person_other_name_entries`), not `name_variants` alone.
"""

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index
import packet
import wikitree
from _lib import person_restricted_names

PID = 'p-de957bcda1'


def _profile(fields: str, aliases: str = f'aliases: [{PID}, Thomas Edward Hartley]\n') -> str:
    return ('---\n'
            f'id: {PID}\n'
            f'{aliases}'
            'name: Thomas Edward Hartley\n'
            f'{fields}'
            'living: false\n'
            'tier: curated\n'
            '---\n\n# Thomas Edward Hartley\n')


class SharedReaderTests(unittest.TestCase):

    def test_every_name_field_is_read(self):
        meta = {'name_variants': [{'value': 'Vee', 'restricted': True}],
                'name_at_birth': {'value': 'Bee', 'restricted': 'dna'},
                'married_name': {'value': 'Em', 'restricted': 'false'},
                'also_known_as': ['Ned', {'value': 'Kay', 'restricted': 'by-request'}]}
        self.assertEqual(person_restricted_names(meta), {'vee', 'bee', 'kay'})


class PacketTests(unittest.TestCase):

    def _redact(self, text, **kw):
        out = packet._redact_profile_text(
            text, include_restricted=kw.get('include_restricted', False),
            include_dna=kw.get('include_dna', False))
        self.assertIsNotNone(out)
        return out[0]

    def test_restricted_birth_married_and_nickname_are_left_out(self):
        text = _profile(
            'name_at_birth: {value: Birthsecret, restricted: true}\n'
            'married_name:\n  value: Marriedsecret\n  restricted: true\n'
            'also_known_as: [Ned, {value: Akasecret, restricted: true}]\n',
            aliases=(f'aliases:\n  - {PID}\n  - Thomas Edward Hartley\n'
                     '  - "[[Birthsecret]]"\n  - Akasecret\n'))
        out = self._redact(text)
        for secret in ('Birthsecret', 'Marriedsecret', 'Akasecret'):
            self.assertNotIn(secret, out)
        self.assertIn('Ned', out)
        self.assertIn('name: Thomas Edward Hartley\n', out)
        self.assertIn('living: false\n', out)

    def test_the_owner_can_include_them_and_by_request_never_ships(self):
        text = _profile('name_at_birth: {value: Birthsecret, restricted: true}\n'
                        'married_name: {value: Marriedsecret, restricted: by-request}\n')
        out = self._redact(text, include_restricted=True)
        self.assertIn('Birthsecret', out)
        self.assertNotIn('Marriedsecret', out)

    def test_public_names_are_kept_byte_for_byte(self):
        text = _profile('name_at_birth: Thomas Hartley\nmarried_name: {value: Tom Easton}\n')
        self.assertEqual(self._redact(text), text)


class WikiTreeTests(unittest.TestCase):

    def test_a_link_to_a_restricted_birth_name_blocks_the_export(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        arch = tmp / 'arch'
        shutil.copytree(ROOT / 'example-archive', arch,
                        ignore=shutil.ignore_patterns('.cache'))
        rec = next(arch.rglob(f'hartley__thomas_edward_{PID}.md'))
        text = rec.read_text(encoding='utf-8')
        self.assertIn('name_variants: [T. E. Hartley]\n', text)
        text = text.replace('name_variants: [T. E. Hartley]\n',
                            'name_variants: [T. E. Hartley]\n'
                            'name_at_birth: {value: Birthsecret, restricted: true}\n', 1)
        text = text.replace('\n## ', '\nAs a boy he went by [[Birthsecret]].\n\n## ', 1)
        rec.write_text(text, encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            index.build_index(arch, {})
            result = wikitree.run_wikitree(arch, PID)
        self.assertEqual(result['status'], 'restricted-names')
        self.assertNotIn('Birthsecret', result['text'] or '')


if __name__ == '__main__':
    unittest.main()
