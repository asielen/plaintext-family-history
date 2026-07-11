"""
test_gitignore.py - the repo's own .gitignore, for the disposable single-file
artifacts a real archive's tools generate.

Codex P1 finding on PR #29: `!example-archive/generated/` (added to ship the
example site showcase) re-includes the WHOLE example-archive/generated/ tree,
and only generated/site-linked/ was ever re-excluded - `generated/gallery/`
and `generated/views/` fell through as trackable even though photoindex.py and
TOOLING.md both describe them as "gitignored - disposable by construction".
A committed gallery/views HTML file embeds file:// hrefs built from whoever
ran the command's own local absolute path (home directory, username, cloud-
sync folder structure) - exactly the leak AGENTS_TOOLING.md's privacy class
warns against. This pins the fix with git's own ignore engine, not a
hand-rolled pattern matcher, so a future .gitignore edit that reopens the gap
is caught immediately.
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _is_ignored(rel_path: str) -> bool:
    """True when `git check-ignore` reports rel_path as ignored."""
    result = subprocess.run(
        ['git', 'check-ignore', '-q', rel_path],
        cwd=ROOT, capture_output=True,
    )
    return result.returncode == 0


@unittest.skipUnless((ROOT / '.git').exists(), 'requires a git checkout')
class ExampleArchiveGeneratedIgnoreTests(unittest.TestCase):
    def test_gallery_and_views_output_stay_ignored(self) -> None:
        for rel in (
            'example-archive/generated/gallery/x.html',
            'example-archive/generated/views/x.html',
        ):
            self.assertTrue(_is_ignored(rel), f'{rel} must stay gitignored')

    def test_site_showcase_stays_trackable(self) -> None:
        # The whole reason for the !example-archive/generated/ carve-out: the
        # showcase build must NOT be caught by the gallery/views re-exclusion.
        self.assertFalse(_is_ignored('example-archive/generated/site/index.html'))

    def test_site_linked_preview_stays_ignored(self) -> None:
        self.assertTrue(_is_ignored('example-archive/generated/site-linked/index.html'))

    def test_example_archive_packet_output_stays_ignored(self) -> None:
        self.assertTrue(_is_ignored('example-archive/out/test-packet.zip'))


if __name__ == '__main__':
    unittest.main()
