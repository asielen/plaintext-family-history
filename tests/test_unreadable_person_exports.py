"""
test_unreadable_person_exports.py - a restricted person whose record file is
not saved as UTF-8 is withheld by every export (#267 review, P1-B).

`fha index` used to SKIP a person file whose bytes would not decode. Skipped,
the person vanished from every exporter's view while other records still named
them, so nothing could withhold them: a `[[P-id|Their Name]]` link in a
relative's profile resolved to nobody and published its display text. The
index now keeps the row the filename gives (name `unknown`, `living:
unknown`), and each exporter's fail-closed marker read withholds it.

One fixture archive, built through a real `fha index`: Thomas, and his mother
Chastina, whose record says `restricted: by-request` but is saved in cp1252.
Thomas's biography links her with her name as the display text, so her name
in the tree and in his prose is what each export must hold back.
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import gedcom
import index
import packet
import wikitree

_spec = importlib.util.spec_from_file_location('fha_site', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
sys.modules.setdefault('fha_site', site)
_spec.loader.exec_module(site)

THOMAS = 'P-0000000001'
CHASTINA = 'P-0000000002'


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def _all_text(root: Path) -> str:
    parts = []
    for p in sorted(root.rglob('*')):
        parts.append(p.name)
        if p.is_file() and p.suffix in ('.html', '.md', '.txt', '.json', '.js', '.ged'):
            parts.append(p.read_text(encoding='utf-8', errors='replace'))
    return '\n'.join(parts)


class Cp1252RestrictedPersonTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'archive'
        _write(self.root / 'fha.yaml', 'archive_name: test\n')
        _write(self.root / 'people' / f'hartley__thomas_{THOMAS}.md',
               f'---\nid: {THOMAS}\nname: Thomas Hartley\ntier: curated\nliving: false\n---\n\n'
               '# Thomas Hartley\n\n## Biography\n\n'
               f'Thomas was raised by his mother [[{CHASTINA}|Chastina Reed]].\n\n'
               '## Stories\n*(none yet)*\n')
        path = self.root / 'people' / f'reed__chastina_augusta_{CHASTINA}.md'
        path.write_bytes(
            (f'---\nid: {CHASTINA}\nname: Chastina Augusta Reed\ntier: stub\n'
             'living: false\nrestricted: by-request\n---\n\n'
             '# Chastina Augusta Reed\n\nBorn in Kraków.\n').encode('cp1252'))
        _write(self.root / 'sources' / 'rel_S-0000000001.md',
               '---\nid: S-0000000001\ntitle: Family bible\nsource_type: other\n'
               'citation: "Family bible."\n---\n\n## Claims\n```yaml\n'
               '- id: C-0000000001\n  type: relationship\n'
               f'  persons: [{THOMAS}, {CHASTINA}]\n'
               f'  roles:\n    child: [{THOMAS}]\n    parent: [{CHASTINA}]\n'
               '  value: "Thomas is her son"\n  status: accepted\n'
               '  reviewed: 2026-01-01\n  confidence: high\n```\n')
        index.build_index(self.root, {})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cp1252_restricted_person_withheld_everywhere(self) -> None:
        with self.subTest(tool='gedcom'):
            r = gedcom.run_gedcom(self.root, None, all_persons=True, include_living=True)
            self.assertEqual(r['status'], 'ok', r['messages'])
            text = r['text'].lower()
            self.assertNotIn('chastina', text)
            self.assertNotIn('/reed/', text)
            # She is in the tree - as a withheld person, not a missing one.
            self.assertIn('1 name /restricted/', text)
            self.assertNotIn(CHASTINA.lower(), text)

        with self.subTest(tool='wikitree'):
            r = wikitree.run_wikitree(self.root, THOMAS.lower())
            self.assertNotIn('chastina', (r['text'] or '').lower())
            # Her own profile is refused outright.
            own = wikitree.run_wikitree(self.root, CHASTINA.lower())
            self.assertIsNone(own['text'])

        with self.subTest(tool='site'):
            out = Path(self._tmp.name) / 'site'
            res = site.run_site(self.root, out)
            self.assertEqual(res['status'], 'ok', res['messages'])
            self.assertFalse((out / 'persons' / f'{CHASTINA.lower()}.html').exists())
            self.assertNotIn('chastina', _all_text(out).lower())
            self.assertTrue((out / 'persons' / f'{THOMAS.lower()}.html').exists())

        with self.subTest(tool='packet'):
            own = packet.run_packet(self.root, CHASTINA.lower(),
                                    Path(self._tmp.name) / 'packets', no_photos=True)
            self.assertNotEqual(own['status'], 'ok', own['messages'])
            self.assertIsNone(own['packet_dir'])


if __name__ == '__main__':
    unittest.main()
