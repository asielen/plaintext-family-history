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
import tempfile
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


@unittest.skipUnless((ROOT / '.git').exists(), 'requires a git checkout')
class ArchiveTemplateAnchoringTests(unittest.TestCase):
    """#57: `archive-template/.gitignore` listed `photos/`, `documents/`,
    `inbox/` UNANCHORED (no leading slash). A gitignore pattern without a
    leading slash matches a directory of that name at ANY depth, so
    unanchored `photos/` also caught `sources/photos/` - the SOURCE RECORDS
    (.md files with YAML frontmatter and claims) that document each photo,
    not the binary asset the pattern was meant for. Reproduced verbatim in a
    real archive:

        $ git check-ignore -v "sources/photos/bob-and-jeanne-1_S-m2hzjek9wv.md"
        .gitignore:9:photos/    sources/photos/bob-and-jeanne-1_S-m2hzjek9wv.md

    Nothing else caught this - `fha lint` was clean, `git status` showed
    nothing, the files read fine on disk. Every source record in that
    archive's `sources/photos/` had silently never been committed.

    Each assertion pair below is two-sided (AGENTS_TOOLING's explicit rule
    for a guard on one half of a symmetric pattern): the source record must
    NOT be ignored, and the binary asset the pattern was actually meant for
    MUST still be ignored - a fix that merely stopped ignoring everything
    would trade one bug for another (binary assets flooding the repo).

    The fixture copies the REAL shipped `archive-template/.gitignore` byte
    for byte (never a hand-retyped copy, which would drift from the file
    this test exists to guard) into a scratch git repo, then asks git's own
    ignore engine - never a hand-rolled pattern matcher - via
    `git check-ignore`, the same principle as the class above and as
    doctor.py's `_check_sources_gitignore` (#57's runtime counterpart)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fixture_root = Path(self._tmp.name)
        subprocess.run(['git', 'init', '-q'], cwd=self.fixture_root, check=True)
        template_gitignore = ROOT / 'archive-template' / '.gitignore'
        (self.fixture_root / '.gitignore').write_text(
            template_gitignore.read_text(encoding='utf-8'), encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _is_ignored_in_fixture(self, rel_path: str) -> bool:
        result = subprocess.run(
            ['git', 'check-ignore', '-q', rel_path],
            cwd=self.fixture_root, capture_output=True,
        )
        return result.returncode == 0

    def test_photo_source_records_stay_trackable(self) -> None:
        self.assertFalse(
            self._is_ignored_in_fixture(
                'sources/photos/bob-and-jeanne-1_S-m2hzjek9wv.md'),
            'a photo SOURCE RECORD under sources/photos/ must stay trackable - #57')

    def test_photo_binary_assets_still_ignored(self) -> None:
        self.assertTrue(
            self._is_ignored_in_fixture('photos/1950/bob-and-jeanne-1.jpg'),
            'the root photos/ binary-asset library must still be gitignored')

    def test_document_source_records_stay_trackable(self) -> None:
        self.assertFalse(
            self._is_ignored_in_fixture('sources/documents/letter_S-1234567890.md'),
            'a document SOURCE RECORD under sources/documents/ must stay trackable - #57')

    def test_document_binary_assets_still_ignored(self) -> None:
        self.assertTrue(
            self._is_ignored_in_fixture('documents/letters/letter.pdf'),
            'the root documents/ binary-asset library must still be gitignored')

    def test_inbox_named_source_folder_stays_trackable(self) -> None:
        self.assertFalse(
            self._is_ignored_in_fixture('sources/inbox/note_S-abcdefghij.md'),
            'a source record under a sources/inbox/-named folder must stay trackable - #57')

    def test_inbox_staging_area_still_ignored(self) -> None:
        self.assertTrue(
            self._is_ignored_in_fixture('inbox/some-scan.jpg'),
            'the root inbox/ staging folder must still be gitignored')


if __name__ == '__main__':
    unittest.main()
