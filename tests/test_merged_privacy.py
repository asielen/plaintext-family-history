"""
test_merged_privacy.py - a restriction on a merged record still governs (#304 review).

SPEC §9 has `fha index` count a claim that names a merged record for the
survivor. That moved the person id the privacy gates check: a duplicate record
marked `restricted: by-request` - someone's request to be left out, made under
the old record - stopped protecting the birth record filed against it, which
then published under the survivor's unrestricted name. The same human, the
same request: every public or shared export must still withhold it.

The fixture: Thomas (the survivor, unrestricted, deceased) and his duplicate,
merged into him and marked `restricted: by-request`. One accepted birth claim
names the duplicate, with his mother Mary.

Run: py -3.14 -m pytest tests/test_merged_privacy.py -q   (from the repo root)
"""

import contextlib
import importlib.util
import io
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

_spec = importlib.util.spec_from_file_location('fha_site_mp', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
sys.modules['fha_site_mp'] = site
_spec.loader.exec_module(site)

THOMAS = 'p-aaaaaaaaa1'
DUP = 'p-aaaaaaaaa2'
MARY = 'p-aaaaaaaaa3'
SRC = 's-bbbbbbbbb1'
SECRET_VALUE = 'Born at the Larkspur Home'


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


class RestrictedTombstoneGovernsTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root / 'fha.yaml', 'roots:\n  documents: documents\n  photos: photos\n')
        _write(self.root / 'people' / f'hartley__thomas_{THOMAS}.md',
               f'---\nid: {THOMAS}\nname: Thomas Hartley\nliving: false\nsex: M\n'
               'tier: curated\n---\n\n# Thomas Hartley\n\n## Biography\n\nA farmer.\n')
        _write(self.root / 'people' / 'stubs'
               / f'MERGED-INTO-{THOMAS}__hartley__thos_{DUP}.md',
               f'---\nid: {DUP}\nname: Thos Hartley\nliving: false\ntier: stub\n'
               f'restricted: by-request\nstatus: merged\nmerged_into: {THOMAS}\n'
               'merge_reason: same man\nmerged_date: 2026-11-01\n---\n\n# Thos Hartley\n')
        _write(self.root / 'people' / 'stubs' / f'cole__mary_{MARY}.md',
               f'---\nid: {MARY}\nname: Mary Cole\nliving: false\nsex: F\n'
               'tier: stub\n---\n\n# Mary Cole\n')
        _write(self.root / 'sources' / 'vital-records' / f'birth_{SRC}.md',
               f'---\nid: {SRC}\ntitle: Birth register\nsource_type: vital-record\n'
               f'citation: "A register."\npeople: [{DUP}]\n---\n\n'
               '## Claims\n```yaml\n'
               f'- id: c-ccccccccc1\n  type: birth\n  value: "{SECRET_VALUE}"\n'
               f'  persons: [{DUP}, {MARY}]\n  roles: {{child: {DUP}, parent: {MARY}}}\n'
               '  date: 1850-03-02\n  status: accepted\n  confidence: high\n'
               '  reviewed: 2026-06-10\n```\n')
        with contextlib.redirect_stdout(io.StringIO()):
            index.build_index(self.root, {'roots': {'documents': 'documents',
                                                    'photos': 'photos'}})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_site_gives_the_survivor_no_page_and_never_prints_the_fact(self) -> None:
        out = self.root / '.cache' / 'site'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            site.run_site(self.root, out, linked=False)
        self.assertFalse((out / 'persons' / f'{THOMAS}.html').exists())
        for page in out.rglob('*'):
            if page.is_file() and page.suffix in ('.html', '.json', '.js'):
                self.assertNotIn(SECRET_VALUE, page.read_text(encoding='utf-8', errors='replace'),
                                 page)

    def test_gedcom_withholds_the_survivor(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            res = gedcom.run_gedcom(self.root, MARY, mode='connected', include_living=True)
        text = res['text'] or ''
        self.assertNotIn('Thomas', text)
        self.assertNotIn('1850', text)

    def test_wikitree_refuses_the_survivor(self) -> None:
        res = wikitree.run_wikitree(self.root, THOMAS)
        self.assertEqual(res['status'], 'restricted-subject', res['messages'])

    def test_packet_refuses_the_survivor_under_every_flag(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            res = packet.run_packet(self.root, THOMAS, self.root / 'out', no_photos=True,
                                    include_restricted=True, include_dna=True)
        self.assertEqual(res['status'], 'restricted-subject', res.get('messages'))
        self.assertFalse((self.root / 'out').exists())


if __name__ == '__main__':
    unittest.main()
