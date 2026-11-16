"""
test_version_sync.py - every document that states the SPEC version states the same one.

SPEC.md's `**Version X.Y - date**` header is the archive format's version
(`scaffold._read_spec_version` stamps it into manifest.json). The TOOLING
docs say "versions track the SPEC", and README states the current version in
its status section. When SPEC moved to 2.0 those four kept saying 1.2, so a
reader could not tell which rules the tools implement (#304 review).

Run: py -3.14 -m pytest tests/test_version_sync.py -q   (from the repo root)
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _spec_version() -> str:
    m = re.search(r'\*\*Version\s+([0-9]+(?:\.[0-9]+)*)',
                  (ROOT / 'SPEC.md').read_text(encoding='utf-8'))
    assert m, 'SPEC.md has no **Version X.Y** header'
    return m.group(1)


class VersionsTrackTheSpecTests(unittest.TestCase):

    def test_tooling_docs_state_the_spec_version(self) -> None:
        version = _spec_version()
        for name in ('TOOLING.md', 'TOOLING_INGESTION.md', 'TOOLING_INTERFACE.md'):
            with self.subTest(doc=name):
                head = (ROOT / name).read_text(encoding='utf-8').splitlines()[:8]
                line = next(ln for ln in head if ln.startswith('**Version'))
                self.assertIn(f'Version {version}', line)
                for stated in re.findall(r'v([0-9]+\.[0-9]+)', line):
                    self.assertEqual(stated, version, line)

    def test_readme_status_states_the_spec_version(self) -> None:
        text = (ROOT / 'README.md').read_text(encoding='utf-8')
        stated = re.findall(r'`spec v([0-9]+\.[0-9]+)`', text)
        self.assertTrue(stated)
        self.assertEqual(set(stated), {_spec_version()})


if __name__ == '__main__':
    unittest.main()
